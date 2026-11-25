"""P3 source commits, explicit cache drift, and bounded repair."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from backend.investigator.baseline import inspect
from backend.lineage import build_lineage, read_lineage_page
from backend.lineage_projection import rebuild_evidence_page
from backend.models import Evidence, HypothesisStep, LineageEdge, SimulatorTarget
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.mark.asyncio
async def test_redteam_source_edges_commit_and_rebuild_without_authority(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(
                tenant_id="a", target_id="checkout", version=1, healthy=False, last_fence=0,
            ))
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "a", "edges", IncidentCreate(target_id="checkout"))
    assert await inspect(sessions_fixture, "a", incident.id) == "SIMULATOR_REVIEW_REQUIRED"
    async with sessions_fixture() as session:
        rows = (await session.scalars(select(LineageEdge))).all()
        assert len(rows) == 5
        assert all(row.tenant_id == "a" and row.incident_id == incident.id for row in rows)
        assert {row.relation for row in rows} == {"CITES", "JUSTIFIED_BY"}
        graph = await build_lineage(session, "a", incident.id)
        assert graph["evidence_projection"] == "MATCHED"
        assert (await read_lineage_page(session, "a", incident.id, "hypothesis"))["evidence_projection"] == "MATCHED"
        assert (await read_lineage_page(session, "a", incident.id, "plan"))["evidence_projection"] == "MATCHED"
        expected_edges = graph["edges"]
    async with sessions_fixture() as session:
        async with session.begin():
            await session.execute(delete(LineageEdge))
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "a", incident.id)
        assert graph["evidence_projection"] == "STALE_OR_MISSING"
        assert graph["edges"] == expected_edges
    async with sessions_fixture() as session:
        async with session.begin():
            assert await rebuild_evidence_page(session, "b", incident.id, "hypothesis") is None
            first = await rebuild_evidence_page(session, "a", incident.id, "hypothesis", limit=1)
            assert first["processed"] == 1 and first["next_cursor"]
    from uuid import UUID
    async with sessions_fixture() as session:
        async with session.begin():
            second = await rebuild_evidence_page(
                session, "a", incident.id, "hypothesis", limit=1, cursor=UUID(first["next_cursor"]),
            )
            assert second == {"processed": 1, "next_cursor": None}
            await rebuild_evidence_page(session, "a", incident.id, "plan")
            await rebuild_evidence_page(session, "a", incident.id, "plan")
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "a", incident.id)
        assert graph["evidence_projection"] == "MATCHED"
        assert len((await session.scalars(select(LineageEdge))).all()) == 5


@pytest.mark.asyncio
async def test_redteam_rebuild_masks_foreign_and_invalid_refs(sessions_fixture):
    async with sessions_fixture() as session:
        own = await admit_incident(session, "a", "own-edge", IncidentCreate(target_id="checkout"))
    async with sessions_fixture() as session:
        other = await admit_incident(session, "b", "other-edge", IncidentCreate(target_id="checkout"))
    async with sessions_fixture() as session:
        async with session.begin():
            from datetime import datetime, timezone
            foreign = uuid4()
            session.add(Evidence(
                id=foreign, tenant_id="b", incident_id=other.id, target_id="checkout",
                source="simulator.health", source_version="simulator-v1", target_version=1,
                observed_at=datetime.now(timezone.utc), sha256="a" * 64, redacted_excerpt="{}",
                classification="INTERNAL", validity="VALID",
            ))
            session.add(HypothesisStep(
                id=uuid4(), incident_id=own.id, step_no=1, model_id="baseline", prompt_version="none",
                evidence_ids=[str(foreign), {"private": "secret"}, str(uuid4())],
                cause_scores={}, probe_id="simulator.health", decision="ABSTAINED",
            ))
    async with sessions_fixture() as session:
        async with session.begin():
            await rebuild_evidence_page(session, "a", own.id, "hypothesis")
    async with sessions_fixture() as session:
        assert (await session.scalars(select(LineageEdge))).all() == []
        graph = await build_lineage(session, "a", own.id)
        assert graph["projection"] == "PARTIAL_LINKS"
        assert str(foreign) not in str(graph)
        assert "secret" not in str(graph)
