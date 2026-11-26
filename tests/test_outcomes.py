
import pytest
from sqlalchemy import select

from backend.adapters.simulator import SimulatorAdapter
from backend.effects.gateway import execute_simulator_effect
from backend.effects.reservations import reserve_simulator_target
from backend.models import (
    EffectReceipt, OutcomeRevision, ResourceEpoch, SimulatorTarget, UIEvent
)
from backend.outcomes import OutcomeConflict, adjudicate_simulator, dispute_outcome
from backend.schemas import IncidentCreate
from backend.services import admit_incident


REVIEWER = "DEV_REVIEWER_CREDENTIAL"


@pytest.fixture
async def witnessed_effect(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(tenant_id="a", target_id="checkout", version=1, healthy=False, last_fence=0))
            session.add(ResourceEpoch(tenant_id="a", target_id="checkout", latest_fence=0, version_seen=1))
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "a", "outcome", IncidentCreate(target_id="checkout"))
    permit = await reserve_simulator_target(sessions_fixture, "a", "checkout", incident.id, "a" * 64)
    adapter = SimulatorAdapter(sessions_fixture)
    assert await execute_simulator_effect(sessions_fixture, adapter, permit) == "VERIFIED"
    async with sessions_fixture() as session:
        receipt = await session.scalar(select(EffectReceipt))
    return sessions_fixture, adapter, incident.id, receipt.id


@pytest.mark.asyncio
async def test_independent_witness_and_dispute_revisions(witnessed_effect):
    sessions, adapter, incident_id, effect_id = witnessed_effect
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"
    async with sessions() as session:
        outcomes = list((await session.scalars(select(OutcomeRevision))).all())
        assert len(outcomes) == 1
        first = outcomes[0]
    async with sessions() as session:
        disputed = await dispute_outcome(
            session, "a", first.id, 1, "independent check disagrees", reviewer_id=REVIEWER,
        )
        assert disputed.label == "DISPUTED" and disputed.revision == 2
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "DISPUTED"
    async with sessions() as session:
        assert len(list((await session.scalars(select(OutcomeRevision))).all())) == 2
        events = (await session.scalars(select(UIEvent).where(
            UIEvent.incident_id == incident_id, UIEvent.kind.in_(("OUTCOME_RECORDED", "OUTCOME_DISPUTED")),
        ).order_by(UIEvent.id))).all()
        assert [event.kind for event in events] == ["OUTCOME_RECORDED", "OUTCOME_DISPUTED"]
        assert {event.tenant_id for event in events} == {"a"}
        assert len({event.projection_version for event in events}) == 1


async def test_failed_dispute_leaves_no_revision_or_notification(witnessed_effect, monkeypatch):
    from backend import outcomes

    sessions, adapter, incident_id, effect_id = witnessed_effect
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"

    async def unavailable(*args):
        raise RuntimeError("synthetic invalidation failure")

    async with sessions() as session:
        first = await session.scalar(select(OutcomeRevision))
        outcome_id = first.id
    monkeypatch.setattr(outcomes, "invalidate_outcome_dependents", unavailable)
    async with sessions() as session:
        with pytest.raises(RuntimeError, match="synthetic invalidation failure"):
            await dispute_outcome(session, "a", outcome_id, 1, "independent check disagrees", reviewer_id=REVIEWER)
    async with sessions() as session:
        assert (await session.scalars(select(OutcomeRevision.label))).all() == ["VERIFIED"]
        assert await session.scalar(select(UIEvent.id).where(
            UIEvent.incident_id == incident_id, UIEvent.kind == "OUTCOME_DISPUTED",
        )) is None


@pytest.mark.asyncio
async def test_stale_revision_and_tenant_scope(witnessed_effect):
    sessions, adapter, incident_id, effect_id = witnessed_effect
    await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id)
    async with sessions() as session:
        first = await session.scalar(select(OutcomeRevision))
    async with sessions() as session:
        assert await dispute_outcome(
            session, "b", first.id, 1, "tenant mismatch", reviewer_id=REVIEWER,
        ) is None
    async with sessions() as session:
        with pytest.raises(OutcomeConflict):
            await dispute_outcome(session, "a", first.id, 5, "stale revision", reviewer_id=REVIEWER)


@pytest.mark.asyncio
async def test_disputing_verified_outcome_quarantines_capsules_and_lessons(witnessed_effect):
    from backend.experiences import capture_capsule, list_capsules, set_consent
    from backend.memory.lessons import create_draft, validate_draft
    from backend.models import ExperienceCapsule, OperationalLesson

    sessions, adapter, incident_id, effect_id = witnessed_effect
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"
    async with sessions() as session:
        await set_consent(session, "a", incident_id, 0, True)
    async with sessions() as session:
        capsule = await capture_capsule(session, "a", incident_id)
    async with sessions() as session:
        lesson = await create_draft(session, "a", capsule.id, "CHECK_VERSION")
    async with sessions() as session:
        await validate_draft(session, "a", lesson.id, 1)
    async with sessions() as session:
        assert len(await list_capsules(session, "a")) == 1
        first = await session.scalar(select(OutcomeRevision))
    async with sessions() as session:
        await dispute_outcome(
            session, "a", first.id, 1, "witness was later independently disputed", reviewer_id=REVIEWER,
        )
    async with sessions() as session:
        assert await list_capsules(session, "a") == []
        capsule_now = await session.get(ExperienceCapsule, capsule.id)
        lesson_now = await session.get(OperationalLesson, lesson.id)
        assert capsule_now.status == "QUARANTINED"
        assert lesson_now.status == "REVOKED"
        assert lesson_now.version == 3


@pytest.mark.asyncio
async def test_outcome_reversal_invalidates_dependent_policy_lineage(witnessed_effect):
    from uuid import uuid4
    from backend.models import (
        EvaluationManifest, FailureCase, FailureCluster, ImprovementCandidate,
        PolicyBundle, PolicyRollout
    )

    sessions, adapter, incident_id, effect_id = witnessed_effect
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"

    cluster_id, candidate_id, eval_id = uuid4(), uuid4(), uuid4()
    bundle_id, rollout_id = uuid4(), uuid4()
    async with sessions() as session:
        async with session.begin():
            first = await session.scalar(select(OutcomeRevision))
            session.add(FailureCluster(
                id=cluster_id, tenant_id="a", signature="test", version=1,
                case_count=1, quarantined=False
            ))
            session.add(FailureCase(
                id=uuid4(), tenant_id="a", incident_id=incident_id,
                outcome_revision_id=first.id, cluster_id=cluster_id,
                origin_code="EFFECT_FAILED", first_bad_step=None,
                classification_version=1, review_status="CLASSIFIED",
            ))
            session.add(ImprovementCandidate(
                id=candidate_id, tenant_id="a", source_cluster_id=cluster_id,
                kind="CONTEXT_LIMIT", base_policy_id="pinned-v1",
                change_spec={"max_context_tokens": 256},
                hypothesis="reduce stale evidence", risk="LOW",
                evaluation_manifest_id="fixture-v1", state="EVALUATION_READY",
                version=2, created_by="local_operator",
            ))
            session.add(EvaluationManifest(
                id=eval_id, tenant_id="a", candidate_id=candidate_id,
                candidate_version=2, candidate_digest="0" * 64,
                incumbent_id="pinned-v1", fixture_digest="f" * 64,
                oracle_version="fixed-v1", trial_budget=2, state="SMOKE_PASS",
            ))
            session.add(PolicyBundle(
                id=bundle_id, tenant_id="a", candidate_id=candidate_id,
                evaluation_manifest_id=eval_id, content_digest="c" * 64,
                content={}, state="DRAFT",
            ))
            session.add(PolicyRollout(
                id=rollout_id, tenant_id="a", bundle_id=bundle_id,
                cohort_percent=1, salt="sandbox", state="DRAFT", version=1,
            ))
        outcome_id = first.id
    async with sessions() as session:
        await dispute_outcome(
            session, "a", outcome_id, 1, "oracle no longer supports result", reviewer_id=REVIEWER,
        )
    async with sessions() as session:
        assert (await session.get(FailureCluster, cluster_id)).quarantined
        assert (await session.get(ImprovementCandidate, candidate_id)).state == "INVALID"
        assert (await session.get(EvaluationManifest, eval_id)).state == "INVALID"
        assert (await session.get(PolicyBundle, bundle_id)).state == "REVOKED"
        rollout = await session.get(PolicyRollout, rollout_id)
        assert rollout.state == "ROLLED_BACK" and rollout.version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fake", [
    (2.0, True),
    (2, 1),
    ("2", True),
    (2,),
])
async def test_redteam_untyped_independent_outcome_cannot_certify_recovery(
    witnessed_effect, fake,
):
    sessions, adapter, incident_id, effect_id = witnessed_effect

    class InvalidWitness(SimulatorAdapter):
        async def observe(self, tenant_id, target_id):
            return fake

    assert await adjudicate_simulator(
        sessions, InvalidWitness(sessions), "a", incident_id, effect_id,
    ) == "DISPUTED"
    async with sessions() as session:
        rows = (await session.scalars(select(OutcomeRevision))).all()
        assert len(rows) == 1 and rows[0].label == "DISPUTED"
        if len(fake) == 2 and type(fake[0]) is int:
            assert rows[0].observed_version == fake[0]
        else:
            assert rows[0].observed_version is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [
    "target", "adapter", "send_count", "observed_version",
])
async def test_redteam_outcome_cannot_be_attributed_to_forged_effect_lineage(
    witnessed_effect, tamper,
):
    sessions, adapter, incident_id, effect_id = witnessed_effect
    async with sessions() as session:
        async with session.begin():
            receipt = await session.get(EffectReceipt, effect_id)
            if tamper == "target":
                receipt.target_id = "unrelated-service"
            elif tamper == "adapter":
                receipt.adapter_id = "external-unqualified"
            elif tamper == "send_count":
                receipt.attempt_count = 0
            else:
                receipt.observed_version += 3

    with pytest.raises(OutcomeConflict, match="incident target"):
        await adjudicate_simulator(
            sessions, adapter, "a", incident_id, effect_id,
        )
    async with sessions() as session:
        assert (await session.scalars(select(OutcomeRevision))).all() == []


@pytest.mark.asyncio
async def test_recorded_recovery_latency_uses_latest_witness_not_projection(witnessed_effect):
    from datetime import datetime, timedelta, timezone

    from backend.models import Incident
    from backend.outcomes import recorded_recovery_timing

    sessions, adapter, incident_id, effect_id = witnessed_effect
    fixed = datetime(2026, 11, 25, tzinfo=timezone.utc)
    async with sessions() as session:
        async with session.begin():
            incident = await session.get(Incident, incident_id)
            incident.created_at = fixed
            incident.status_projection = "VERIFIED"
    async with sessions() as session:
        report = await recorded_recovery_timing(session, "a", incident_id)
        assert report["admission_to_recorded_witness_ms"] is None
        assert report["latest_outcome_revision"] is None
        assert await recorded_recovery_timing(session, "b", incident_id) is None

    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"
    async with sessions() as session:
        async with session.begin():
            outcome = await session.scalar(select(OutcomeRevision))
            outcome.created_at = fixed + timedelta(seconds=3, milliseconds=125)
            outcome_id = outcome.id
    async with sessions() as session:
        report = await recorded_recovery_timing(session, "a", incident_id)
        assert report == {
            "scope": "SIMULATOR_LEDGER_ONLY",
            "latest_outcome_revision": 1,
            "latest_outcome_label": "VERIFIED",
            "admission_to_recorded_witness_ms": 3125,
            "production_recovery_certified": False,
        }
    async with sessions() as session:
        await dispute_outcome(session, "a", outcome_id, 1, "witness disputed in lab", reviewer_id=REVIEWER)
    async with sessions() as session:
        report = await recorded_recovery_timing(session, "a", incident_id)
        assert report["latest_outcome_revision"] == 2
        assert report["latest_outcome_label"] == "DISPUTED"
        assert report["admission_to_recorded_witness_ms"] is None


@pytest.mark.asyncio
async def test_recorded_recovery_timing_rejects_wrong_witness_and_negative_duration(
    witnessed_effect,
):
    from datetime import datetime, timedelta, timezone

    from backend.models import Incident
    from backend.outcomes import recorded_recovery_timing

    sessions, adapter, incident_id, effect_id = witnessed_effect
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"
    fixed = datetime(2026, 11, 25, tzinfo=timezone.utc)
    async with sessions() as session:
        async with session.begin():
            incident = await session.get(Incident, incident_id)
            incident.created_at = fixed
            outcome = await session.scalar(select(OutcomeRevision))
            outcome.created_at = fixed - timedelta(seconds=1)
    async with sessions() as session:
        report = await recorded_recovery_timing(session, "a", incident_id)
        assert report["admission_to_recorded_witness_ms"] is None
    async with sessions() as session:
        async with session.begin():
            outcome = await session.scalar(select(OutcomeRevision))
            outcome.created_at = fixed + timedelta(seconds=1)
            outcome.witness_source = "OPERATOR_DISPUTE"
    async with sessions() as session:
        report = await recorded_recovery_timing(session, "a", incident_id)
        assert report["admission_to_recorded_witness_ms"] is None


@pytest.mark.asyncio
async def test_outcome_history_includes_latest_dispute_after_page_limit(sessions_fixture):
    from uuid import uuid4

    from httpx import ASGITransport, AsyncClient

    from backend.api import app
    from backend.database import get_session
    from backend.settings import Settings, get_settings

    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "a", "long-outcome-history", IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture.begin() as session:
        for revision in range(1, 52):
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="a", incident_id=incident.id, revision=revision,
                label="DISPUTED" if revision == 51 else "VERIFIED",
                witness_source="SIMULATOR_WITNESS", reason="synthetic witness history",
            ))

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(local_token="history-test-token")
    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/v1/incidents/{incident.id}/outcomes", headers={
                "X-Tenant-Id": "a", "X-OpsBlaze-Token": "history-test-token",
            })
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    history = response.json()
    assert [row["revision"] for row in history] == list(range(2, 52))
    assert history[-1]["label"] == "DISPUTED"


@pytest.mark.asyncio
async def test_redteam_recovery_timing_api_requires_tenant_and_token(witnessed_effect):
    from httpx import ASGITransport, AsyncClient

    from backend.api import app
    from backend.database import get_session
    from backend.settings import Settings, get_settings

    sessions, _, incident_id, _ = witnessed_effect

    async def override_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(local_token="timing-test-token")
    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            path = f"/v1/incidents/{incident_id}/recovery-timing"
            own = {"X-Tenant-Id": "a", "X-OpsBlaze-Token": "timing-test-token"}
            assert (await client.get(path, headers=own)).status_code == 200
            assert (await client.get(path, headers={**own, "X-Tenant-Id": "b"})).status_code == 404
            assert (await client.get(path, headers={**own, "X-OpsBlaze-Token": "wrong"})).status_code == 401
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_recorded_witness_span_links_exact_immutable_outcome(witnessed_effect, monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from backend.models import TraceRef
    from backend.trace_refs import read_trace

    sessions, adapter, incident_id, effect_id = witnessed_effect
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("backend.telemetry.TRACER", provider.get_tracer("witness"))
    try:
        assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"
        assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"
        async with sessions() as session:
            outcome = await session.scalar(select(OutcomeRevision))
            refs = (await session.scalars(select(TraceRef).where(
                TraceRef.record_type == "outcome",
            ))).all()
            assert len(refs) == 1 and refs[0].record_id == outcome.id
            observed = next(span for span in exporter.get_finished_spans()
                            if span.name == "opsblaze.outcome.observe")
            assert refs[0].span_id == f"{observed.context.span_id:016x}"
            report = await read_trace(session, "a", incident_id, refs[0].trace_id)
            assert report["references"][0]["node_id"] == f"outcome:{outcome.id}"
            assert not report["collector_verified"]
            assert await read_trace(session, "b", incident_id, refs[0].trace_id) is None
    finally:
        provider.shutdown()


@pytest.mark.asyncio
async def test_outcome_projection_preserves_both_revisions_and_rebuilds(witnessed_effect):
    from sqlalchemy import delete
    from backend.models import LineageEdge
    from backend.lineage import build_lineage
    from backend.lineage_projection import rebuild_evidence_page

    sessions, adapter, incident_id, effect_id = witnessed_effect
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"
    async with sessions() as session:
        first_id = await session.scalar(select(OutcomeRevision.id))
    async with sessions() as session:
        await dispute_outcome(
            session, "a", first_id, 1, "synthetic observation disagrees", reviewer_id=REVIEWER,
        )
    async with sessions() as session:
        rows = (await session.scalars(select(LineageEdge).where(LineageEdge.from_type == "outcome"))).all()
        source_ids = set((await session.scalars(select(OutcomeRevision.id))).all())
        assert len(rows) == 2 and {r.from_id for r in rows} == source_ids
        assert all(r.to_id == effect_id and r.relation == "WITNESSES" for r in rows)
        graph = await build_lineage(session, "a", incident_id)
        assert graph["evidence_projection"] == "MATCHED"
    async with sessions() as session:
        async with session.begin():
            await session.execute(delete(LineageEdge).where(LineageEdge.from_type == "outcome"))
    async with sessions() as session:
        assert (await build_lineage(session, "a", incident_id))["evidence_projection"] == "STALE_OR_MISSING"
    async with sessions() as session:
        async with session.begin():
            assert (await rebuild_evidence_page(session, "a", incident_id, "outcome"))["processed"] == 2
            assert (await rebuild_evidence_page(session, "a", incident_id, "outcome"))["processed"] == 2
    async with sessions() as session:
        assert (await build_lineage(session, "a", incident_id))["evidence_projection"] == "MATCHED"
