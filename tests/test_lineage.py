
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.investigator.baseline import inspect
from backend.lineage import build_lineage
from backend.models import HypothesisStep, SimulatorTarget
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.mark.asyncio
async def test_tenant_scoped_lineage_root(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "root", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "t", incident.id)
        assert graph["projection"] == "COMPLETE"
        assert graph["nodes"][0]["id"] == f"incident:{incident.id}"
        assert graph["trace_spans_available"] is False
        assert await build_lineage(session, "other", incident.id) is None


@pytest.mark.asyncio
async def test_evidence_to_plan_links_are_persistently_reconstructed(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(
                SimulatorTarget(
                    tenant_id="t", target_id="checkout",
                    version=1, healthy=False, last_fence=0,
                )
            )
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "link", IncidentCreate(target_id="checkout")
        )
    assert await inspect(sessions_fixture, "t", incident.id) == "SIMULATOR_REVIEW_REQUIRED"
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "t", incident.id)
        assert graph["projection"] == "COMPLETE"
        assert len([n for n in graph["nodes"] if n["kind"] == "evidence"]) == 2
        assert len([n for n in graph["nodes"] if n["kind"] == "plan"]) == 1
        assert len([e for e in graph["edges"] if e["relation"] == "JUSTIFIED_BY"]) == 2


@pytest.mark.asyncio
async def test_missing_evidence_reference_is_reported_not_invented(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(
                SimulatorTarget(
                    tenant_id="t", target_id="checkout",
                    version=1, healthy=False, last_fence=0,
                )
            )
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "broken", IncidentCreate(target_id="checkout")
        )
    await inspect(sessions_fixture, "t", incident.id)
    phantom = str(uuid4())
    async with sessions_fixture() as session:
        async with session.begin():
            step = await session.scalar(
                select(HypothesisStep).where(
                    HypothesisStep.incident_id == incident.id,
                    HypothesisStep.step_no == 1,
                )
            )
            step.evidence_ids = [phantom]
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "t", incident.id)
        assert graph["projection"] == "PARTIAL_LINKS"
        assert any(m["missing_target"] == f"evidence:{phantom}" for m in graph["missing_refs"])
        assert not any(
            n["id"] == f"evidence:{phantom}" for n in graph["nodes"]
        )
