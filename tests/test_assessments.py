
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from backend.api import app, get_session, local_tenant
from backend.investigator.abstain import assess_decision
from backend.models import (
    DecisionAssessment, Evidence, IncidentPlan, ProbeCall, SimulatorTarget,
)
from backend.probes.gateway import invoke_probe
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings
from backend.workflows import activities


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


@pytest.mark.asyncio
async def test_rehashed_but_false_observation_abstains_before_plan(assessment_lab):
    sessions, incident_id, ids = assessment_lab
    async with sessions() as session:
        async with session.begin():
            evidence = await session.get(Evidence, UUID(ids[0]))
            evidence.redacted_excerpt = '{"healthy":true}'
            evidence.sha256 = hashlib.sha256(
                evidence.redacted_excerpt.encode()
            ).hexdigest()
    assessment = await assess_decision(sessions, "t", incident_id, 2, ids)
    assert assessment.outcome == "ABSTAINED"
    assert assessment.reason == "INVALID_OBSERVATION"


@pytest.mark.asyncio
async def test_probe_link_change_abstains(assessment_lab):
    sessions, incident_id, ids = assessment_lab
    async with sessions() as session:
        async with session.begin():
            call = await session.scalar(
                select(ProbeCall).where(
                    ProbeCall.incident_id == incident_id, ProbeCall.step_no == 1,
                )
            )
            call.evidence_id = UUID(ids[1])
    assessment = await assess_decision(sessions, "t", incident_id, 2, ids)
    assert assessment.outcome == "ABSTAINED"
    assert assessment.reason == "UNLINKED_EVIDENCE"


@pytest.mark.asyncio
async def test_future_dated_evidence_abstains(assessment_lab):
    sessions, incident_id, ids = assessment_lab
    async with sessions() as session:
        async with session.begin():
            evidence = await session.get(Evidence, UUID(ids[0]))
            evidence.observed_at = datetime.now(timezone.utc) + timedelta(days=1)
    assessment = await assess_decision(sessions, "t", incident_id, 2, ids)
    assert assessment.outcome == "ABSTAINED"
    assert assessment.reason == "STALE_EVIDENCE"


@pytest.mark.asyncio
async def test_assessment_cannot_be_reused_for_another_evidence_snapshot(assessment_lab):
    sessions, incident_id, ids = assessment_lab
    accepted = await assess_decision(sessions, "t", incident_id, 2, ids)
    assert accepted.outcome == "PROPOSED"
    assert await assess_decision(
        sessions, "t", incident_id, 2, list(reversed(ids))
    ) is None
    async with sessions() as session:
        saved = await session.scalar(
            select(DecisionAssessment).where(
                DecisionAssessment.incident_id == incident_id,
                DecisionAssessment.step_no == 2,
            )
        )
        assert saved.id == accepted.id
        assert saved.evidence_digest == accepted.evidence_digest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["late_denied_probe", "target_version", "evidence_hash", "source", "future_time"],
)
async def test_redteam_positive_assessment_replay_rechecks_current_safety(
    assessment_lab, damage,
):
    sessions, incident_id, evidence_ids = assessment_lab
    accepted = await assess_decision(sessions, "t", incident_id, 2, evidence_ids)
    assert accepted.outcome == "PROPOSED"

    if damage == "late_denied_probe":
        denied = await invoke_probe(
            sessions, "t", incident_id, 3, "shell.exec",
        )
        assert denied.outcome == "DENIED"
    else:
        async with sessions() as session:
            async with session.begin():
                if damage == "target_version":
                    target = await session.get(SimulatorTarget, ("t", "checkout"))
                    target.version += 1
                else:
                    evidence = await session.get(Evidence, UUID(evidence_ids[0]))
                    if damage == "evidence_hash":
                        evidence.redacted_excerpt = '{"healthy":true}'
                    elif damage == "source":
                        evidence.source = "unknown.telemetry"
                    else:
                        evidence.observed_at = datetime.now(timezone.utc) + timedelta(days=1)

    assert await assess_decision(
        sessions, "t", incident_id, 2, evidence_ids,
    ) is None
    async with sessions() as session:
        persisted = await session.scalar(
            select(DecisionAssessment).where(
                DecisionAssessment.incident_id == incident_id,
                DecisionAssessment.step_no == 2,
            )
        )
        assert persisted.id == accepted.id
        assert persisted.outcome == "PROPOSED"  # immutable forensic history


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("source_version", "untrusted-provider-v9"),
    ("classification", "PUBLIC"),
])
async def test_redteam_untrusted_probe_metadata_abstains_at_first_assessment(
    assessment_lab, field, value,
):
    sessions, incident_id, ids = assessment_lab
    async with sessions() as session:
        async with session.begin():
            evidence = await session.get(Evidence, UUID(ids[0]))
            setattr(evidence, field, value)
    assessment = await assess_decision(sessions, "t", incident_id, 2, ids)
    assert assessment.outcome == "ABSTAINED"
    assert assessment.reason == "INVALID_EVIDENCE"


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "expected_step", "reason"), [
    ("target_removed", 1, "TOOL_UNAVAILABLE"),
    ("budget_reduced", 2, "BUDGET_EXHAUSTED"),
    ("different_recorded_probe", 1, "TOOL_BLOCKED"),
    ("target_recovered", 2, "TARGET_ALREADY_HEALTHY"),
])
async def test_activity_handoff_persists_typed_assessment_on_retry(
    sessions_fixture, monkeypatch, failure, expected_step, reason,
):
    async with sessions_fixture.begin() as session:
        session.add(SimulatorTarget(
            tenant_id="t", target_id="checkout", version=1, healthy=False, last_fence=0,
        ))
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "runtime-handoff", IncidentCreate(target_id="checkout"),
        )

    settings = Settings(ai_advisory_enabled=False, max_probe_calls_per_incident=3)
    monkeypatch.setattr(activities, "get_session_maker", lambda: sessions_fixture)
    monkeypatch.setattr(activities, "get_settings", lambda: settings)
    for module in ("routing.decision", "probes.gateway", "investigator.abstain", "investigator.advisory"):
        monkeypatch.setattr(f"backend.{module}.get_settings", lambda: settings)

    assert await activities.select_incident_route("t", str(incident.id)) == "RUNBOOK"
    if failure == "target_removed":
        async with sessions_fixture.begin() as session:
            target = await session.get(SimulatorTarget, ("t", "checkout"))
            await session.delete(target)
    elif failure == "budget_reduced":
        settings.max_probe_calls_per_incident = 1
    elif failure == "different_recorded_probe":
        await invoke_probe(sessions_fixture, "t", incident.id, 1, "simulator.version")
    else:
        await invoke_probe(sessions_fixture, "t", incident.id, 1, "simulator.health")
        async with sessions_fixture.begin() as session:
            target = await session.get(SimulatorTarget, ("t", "checkout"))
            target.healthy = True

    async def session_override():
        async with sessions_fixture() as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_session, session_override)
    monkeypatch.setitem(app.dependency_overrides, local_tenant, lambda: "t")
    route = f"/v1/incidents/{incident.id}/assessments"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert await activities.inspect_incident_read_only("t", str(incident.id)) == "ABSTAINED"
        first = await client.get(route)
        assert first.status_code == 200
        [assessment] = first.json()
        assert (assessment["step_no"], assessment["outcome"], assessment["reason"]) == (
            expected_step, "ABSTAINED", reason,
        )
        assert await activities.inspect_incident_read_only("t", str(incident.id)) == "ABSTAINED"
        repeated = await client.get(route)
        assert repeated.status_code == 200 and repeated.json() == first.json()

    async with sessions_fixture() as session:
        assert await session.scalar(select(IncidentPlan.id).where(
            IncidentPlan.incident_id == incident.id,
        )) is None


@pytest.mark.asyncio
async def test_probe_handoff_cannot_reuse_a_positive_assessment(assessment_lab):
    sessions, incident_id, ids = assessment_lab
    accepted = await assess_decision(sessions, "t", incident_id, 2, ids)
    assert await assess_decision(
        sessions, "t", incident_id, 2, ids, handoff_reason="TOOL_BLOCKED",
    ) is None
    async with sessions() as session:
        persisted = await session.get(DecisionAssessment, accepted.id)
        assert persisted.outcome == "PROPOSED" and persisted.evidence_digest == accepted.evidence_digest
