
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.learning.failures import (
    FailureConflict, draft_candidate, record_case, validate_change,
)
from backend.models import (
    FailureCase, FailureCluster, OutcomeRevision,
)
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.fixture
async def failure_case(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "failure", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        async with session.begin():
            outcome = OutcomeRevision(
                id=uuid4(), tenant_id="t", incident_id=incident.id,
                effect_id=None, revision=1, label="DISPUTED",
                witness_source="OPERATOR_DISPUTE",
                observed_version=None, reason="reviewed discrepancy",
            )
            session.add(outcome)
        outcome_id = outcome.id
    async with sessions_fixture() as session:
        case = await record_case(session, "t", outcome_id)
    return sessions_fixture, incident.id, outcome_id, case


@pytest.mark.asyncio
async def test_dispute_classifies_as_uncertain_and_is_idempotent(failure_case):
    sessions, incident_id, outcome_id, first = failure_case
    assert first.origin_code == "OUTCOME_DISPUTED"
    assert first.review_status == "UNCERTAIN"
    async with sessions() as session:
        duplicate = await record_case(session, "t", outcome_id)
        assert duplicate.id == first.id
    async with sessions() as session:
        cluster = await session.get(FailureCluster, first.cluster_id)
        assert cluster.case_count == 1
        assert len((await session.scalars(select(FailureCase))).all()) == 1


def test_change_validator_rejects_permission_or_code_edits():
    for kind, spec in (
        ("PROBE_ORDER", {"order": ["shell.exec"]}),
        ("CONTEXT_LIMIT", {"max_context_tokens": 1000}),
        ("ROUTE_SWITCH", {"strategy": "autonomous", "permissions": ["admin"]}),
        ("MIGRATION", {"sql": "ALTER TABLE incident"}),
        ("PROBE_ORDER", {"order": [{"tool": "simulator.health"}]}),
    ):
        with pytest.raises(FailureConflict):
            validate_change(kind, spec)
    validate_change("PROBE_ORDER", {"order": ["simulator.health"]})
    validate_change("ROUTE_SWITCH", {"strategy": "no_memory"})


@pytest.mark.parametrize("strategy", [[], {}], ids=["array", "object"])
@pytest.mark.asyncio
async def test_improvement_draft_rejects_non_string_strategy(sessions_fixture, strategy):
    from httpx import ASGITransport, AsyncClient

    from backend.api import app
    from backend.database import get_session
    from backend.settings import Settings, get_settings

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(local_token="draft-test-token")
    app.dependency_overrides[get_session] = override_session
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/improvements/draft", headers={
                "X-Tenant-Id": "t", "X-OpsBlaze-Token": "draft-test-token",
            }, json={
                "source_cluster_id": str(uuid4()), "kind": "ROUTE_SWITCH",
                "base_policy_id": "baseline-v1", "change_spec": {"strategy": strategy},
                "hypothesis": "reduce irrelevant context", "risk": "LOW",
                "evaluation_manifest_id": "fixture-v1",
            })
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json() == {"detail": "change class or content is not allowlisted"}


@pytest.mark.asyncio
async def test_uncertain_cluster_cannot_submit_unreviewed_proposal(failure_case):
    sessions, _, _, case = failure_case
    async with sessions() as session:
        with pytest.raises(FailureConflict):
            await draft_candidate(
                session, "t", case.cluster_id, "CONTEXT_LIMIT",
                "baseline-v1", {"max_context_tokens": 256},
                "reduce irrelevant evidence", "LOW", "fixture-v1",
            )


@pytest.mark.asyncio
async def test_old_outcome_revision_cannot_create_failure_case(sessions_fixture):
    from uuid import uuid4
    from backend.learning.failures import FailureConflict, record_case
    from backend.models import OutcomeRevision
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "revision", IncidentCreate(target_id="checkout")
        )
    earlier, later = uuid4(), uuid4()
    async with sessions_fixture() as session:
        async with session.begin():
            session.add_all([
                OutcomeRevision(
                    id=earlier, tenant_id="t", incident_id=incident.id,
                    effect_id=None, revision=1, label="DISPUTED",
                    witness_source="OPERATOR_DISPUTE", observed_version=None,
                    reason="original uncertain label",
                ),
                OutcomeRevision(
                    id=later, tenant_id="t", incident_id=incident.id,
                    effect_id=None, revision=2, label="DISPUTED",
                    witness_source="OPERATOR_DISPUTE", observed_version=None,
                    reason="newer independent check",
                ),
            ])
    async with sessions_fixture() as session:
        with pytest.raises(FailureConflict, match="superseded"):
            await record_case(session, "t", earlier)
    async with sessions_fixture() as session:
        current = await record_case(session, "t", later)
        assert current.outcome_revision_id == later


@pytest.mark.asyncio
async def test_candidate_submission_rechecks_latest_source_revision(sessions_fixture):
    from uuid import uuid4
    from backend.learning.failures import (
        FailureConflict, record_case, draft_candidate, submit_candidate,
    )
    from backend.models import FailureCase, OutcomeRevision
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "candidate", IncidentCreate(target_id="checkout")
        )
    first_outcome = uuid4()
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=first_outcome, tenant_id="t", incident_id=incident.id,
                effect_id=None, revision=1, label="DISPUTED",
                witness_source="OPERATOR_DISPUTE", observed_version=None,
                reason="reported uncertain effect",
            ))
    async with sessions_fixture() as session:
        case = await record_case(session, "t", first_outcome)
    async with sessions_fixture() as session:
        async with session.begin():
            source = await session.get(FailureCase, case.id)
            source.review_status = "CLASSIFIED"

    async with sessions_fixture() as session:
        candidate = await draft_candidate(
            session, "t", case.cluster_id,
            "CONTEXT_LIMIT", "baseline-v1", {"max_context_tokens": 256},
            "safe context window for simulator evidence", "LOW", "fixed-v1",
        )

    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="t", incident_id=incident.id,
                effect_id=None, revision=2, label="DISPUTED",
                witness_source="OPERATOR_DISPUTE", observed_version=None,
                reason="revised outcome after draft",
            ))
    async with sessions_fixture() as session:
        with pytest.raises(FailureConflict, match="superseded"):
            await submit_candidate(session, "t", candidate.id, 1)


@pytest.mark.asyncio
async def test_failure_lineage_is_transactional_and_reconstructable(failure_case):
    from sqlalchemy import delete
    from backend.lineage import build_lineage, read_lineage_page
    from backend.lineage_projection import rebuild_evidence_page
    from backend.models import LineageEdge

    sessions, incident_id, outcome_id, case = failure_case
    async with sessions() as session:
        row = await session.scalar(select(LineageEdge).where(LineageEdge.from_type == "failure"))
        assert (row.from_id, row.to_id, row.relation) == (case.id, outcome_id, "CLASSIFIES")
        assert (await build_lineage(session, "t", incident_id))["evidence_projection"] == "MATCHED"
    async with sessions() as session:
        async with session.begin():
            await session.execute(delete(LineageEdge).where(LineageEdge.from_type == "failure"))
    async with sessions() as session:
        assert (await read_lineage_page(session, "t", incident_id, "failure"))["evidence_projection"] == "STALE_OR_MISSING"
    async with sessions() as session:
        async with session.begin():
            assert await rebuild_evidence_page(session, "other", incident_id, "failure") is None
            assert (await rebuild_evidence_page(session, "t", incident_id, "failure"))["processed"] == 1
    async with sessions() as session:
        assert (await build_lineage(session, "t", incident_id))["evidence_projection"] == "MATCHED"


@pytest.mark.asyncio
async def test_root_step_clustering_separates_counterexamples_and_attributes_owners(
    sessions_fixture,
):
    import asyncio
    from uuid import uuid4
    from backend.learning.failures import (
        list_clusters_page, list_failures_page, list_improvements_page, submit_candidate,
    )
    from backend.models import ProbeCall

    async with sessions_fixture() as session:
        inc_a = await admit_incident(session, "t-e3", "e3-a", IncidentCreate(target_id="svc-a"))
    async with sessions_fixture() as session:
        inc_b = await admit_incident(session, "t-e3", "e3-b", IncidentCreate(target_id="svc-b"))
    async with sessions_fixture() as session:
        inc_c = await admit_incident(session, "t-e3", "e3-c", IncidentCreate(target_id="svc-c"))

    out_a, out_b, out_c = uuid4(), uuid4(), uuid4()
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(ProbeCall(
                id=uuid4(), incident_id=inc_a.id, step_no=1,
                catalog_id="simulator.health", outcome="DENIED",
            ))
            session.add(ProbeCall(
                id=uuid4(), incident_id=inc_b.id, step_no=2,
                catalog_id="simulator.version", outcome="DENIED",
            ))
            session.add(OutcomeRevision(
                id=out_a, tenant_id="t-e3", incident_id=inc_a.id, effect_id=None,
                revision=1, label="DISPUTED", witness_source="OPERATOR_DISPUTE",
                observed_version=None, reason="step 1 probe denied",
            ))
            session.add(OutcomeRevision(
                id=out_b, tenant_id="t-e3", incident_id=inc_b.id, effect_id=None,
                revision=1, label="DISPUTED", witness_source="OPERATOR_DISPUTE",
                observed_version=None, reason="step 2 probe denied",
            ))
            session.add(OutcomeRevision(
                id=out_c, tenant_id="t-e3", incident_id=inc_c.id, effect_id=None,
                revision=1, label="DISPUTED", witness_source="OPERATOR_DISPUTE",
                observed_version=None, reason="counterexample duplicate step",
            ))

    async with sessions_fixture() as session:
        case_a = await record_case(session, "t-e3", out_a)
    async with sessions_fixture() as session:
        case_b = await record_case(session, "t-e3", out_b)
    assert case_a.cluster_id != case_b.cluster_id
    assert case_a.first_bad_step == 1
    assert case_b.first_bad_step == 2

    # Concurrent duplicate record_case requests must stay idempotent with case_count == 1.
    async def classify_c():
        async with sessions_fixture() as s:
            try:
                return await record_case(s, "t-e3", out_c)
            except Exception:
                return None

    await asyncio.gather(classify_c(), classify_c())
    async with sessions_fixture() as session:
        case_c = await record_case(session, "t-e3", out_c)
        assert case_c is not None
        async with session.begin():
            case_a_row = await session.get(FailureCase, case_a.id)
            case_a_row.review_status = "CLASSIFIED"
        cand = await draft_candidate(
            session, "t-e3", case_a.cluster_id, "PROBE_ORDER", "deterministic-v1",
            {"order": ["simulator.health"]}, "attributed candidate hypothesis", "LOW",
            "smoke-v1", created_by="alice@lab",
        )
        submitted = await submit_candidate(
            session, "t-e3", cand.id, 1, reviewer_id="bob@reviewer",
        )
        assert submitted.created_by == "alice@lab"
        assert submitted.reviewed_by == "bob@reviewer"
        c_rows, _ = await list_clusters_page(session, "t-e3", limit=2)
        f_rows, _ = await list_failures_page(session, "t-e3", limit=2)
        i_rows, _ = await list_improvements_page(session, "t-e3", limit=2)
        assert len(c_rows) == 2 and len(f_rows) == 2 and len(i_rows) == 1
