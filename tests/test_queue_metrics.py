"""B4: durable queue p95, aged work and retry pressure with tenant isolation."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.api import app
from backend.database import get_session
from backend.models import EffectReceipt, Incident, OutboxEntry
from backend.operations import queue_health
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings, get_settings


@pytest.mark.asyncio
async def test_redteam_queue_health_exact_p95_and_tenant_scope(sessions_fixture):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    own_ids = []
    for index in range(5):
        async with sessions_fixture() as session:
            incident = await admit_incident(
                session, "alpha", f"queue-{index}",
                IncidentCreate(target_id="checkout"),
            )
            own_ids.append(incident.id)
    async with sessions_fixture() as session:
        foreign = await admit_incident(
            session, "beta", "separate-tenant",
            IncidentCreate(target_id="private-target"),
        )

    async with sessions_fixture() as session:
        async with session.begin():
            for index, incident_id in enumerate(own_ids):
                age = now - timedelta(minutes=(index + 1) * 10)
                stored = await session.get(Incident, incident_id)
                stored.phase_entered_at = age
                intent = await session.scalar(
                    select(OutboxEntry)
                    .where(OutboxEntry.incident_id == incident_id)
                )
                intent.created_at = age
                intent.attempt = index + 1
            another = await session.get(Incident, foreign.id)
            another.phase_entered_at = now - timedelta(days=2)
            beta_intent = await session.scalar(
                select(OutboxEntry)
                .where(OutboxEntry.incident_id == foreign.id)
            )
            beta_intent.created_at = now - timedelta(days=2)
            beta_intent.attempt = 99

    async with sessions_fixture() as session:
        health = await queue_health(session, "alpha", 900, now)
        assert health == {
            "pending_outbox": 5,
            "experience_dead_letters_unreviewed": 0,
            "active_incidents": 5,
            "unresolved_effects": 0,
            "aged_unresolved_effects": 0,
            "pending_approvals": 0,
            "aged_pending_approvals": 0,
            "oldest_pending_approval_seconds": 0,
            "pending_approval_age_p95_seconds": 0,
            "oldest_pending_seconds": 3000,
            "oldest_active_phase_seconds": 3000,
            "pending_age_p95_seconds": 3000,
            "aged_pending_outbox": 4,
            "aged_active_incidents": 4,
            "max_pending_attempts": 5,
            "stuck_after_seconds": 900,
        }
    async with sessions_fixture() as session:
        health = await queue_health(session, "beta", 900, now)
        assert health["pending_outbox"] == 1
        assert health["max_pending_attempts"] == 99

    async with sessions_fixture() as session:
        async with session.begin():
            for incident_id in own_ids:
                intent = await session.scalar(
                    select(OutboxEntry)
                    .where(OutboxEntry.incident_id == incident_id)
                )
                intent.delivered_at = now

    async with sessions_fixture() as session:
        health = await queue_health(session, "alpha", 900, now)
        assert health["pending_outbox"] == 0
        assert health["oldest_pending_seconds"] == 0
        assert health["pending_age_p95_seconds"] == 0
        assert health["aged_pending_outbox"] == 0
        assert health["max_pending_attempts"] == 0

    async with sessions_fixture() as session:
        with pytest.raises(ValueError, match="positive"):
            await queue_health(session, "alpha", 0, now)
        with pytest.raises(ValueError, match="timezone"):
            await queue_health(session, "alpha", 900, datetime.now())


@pytest.mark.asyncio
async def test_redteam_queue_api_never_exposes_other_tenant_data(sessions_fixture):
    async with sessions_fixture() as session:
        await admit_incident(
            session, "alpha", "own-queue", IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        await admit_incident(
            session, "beta", "foreign-queue", IncidentCreate(target_id="private"),
        )

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(
        local_token="queue-metrics-test", stuck_after_seconds=900,
    )
    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            own = {"X-Tenant-Id": "alpha", "X-OpsBlaze-Token": "queue-metrics-test"}
            response = await client.get("/v1/operations/queues", headers=own)
            assert response.status_code == 200, response.text
            report = response.json()
            assert report["pending_outbox"] == 1
            assert report["active_incidents"] == 1
            assert report["stuck_after_seconds"] == 900
            assert all(type(value) is int for value in report.values())
            assert "private" not in str(report)
            denied = await client.get(
                "/v1/operations/queues",
                headers={"X-Tenant-Id": "alpha", "X-OpsBlaze-Token": "wrong"},
            )
            assert denied.status_code == 401
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("effect_state", ["SENT", "UNKNOWN", "CONFIRMED"])
async def test_redteam_unresolved_effect_is_reported_as_aged_work(
    sessions_fixture, monkeypatch, effect_state,
):
    from backend.workers.state_age import scan_stuck

    old = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "alpha", "unknown-effect",
            IncidentCreate(target_id="checkout"),
        )
    effect_id = uuid4()
    async with sessions_fixture() as session:
        async with session.begin():
            incident_row = await session.get(Incident, incident.id)
            incident_row.phase_entered_at = old
            outbox = await session.scalar(
                select(OutboxEntry).where(OutboxEntry.incident_id == incident.id)
            )
            outbox.created_at = old
            session.add(EffectReceipt(
                id=effect_id, tenant_id="alpha", incident_id=incident.id,
                plan_digest="d" * 64, target_id="checkout",
                adapter_id="simulator", idempotency_key=str(effect_id),
                fence=1, expected_version=1, state=effect_state,
                observed_version=2 if effect_state == "CONFIRMED" else None,
                attempt_count=1, updated_at=old,
            ))

    async with sessions_fixture() as session:
        health = await queue_health(session, "alpha", 300)
        assert health["unresolved_effects"] == 1
        assert health["aged_unresolved_effects"] == 1
    monkeypatch.setattr(
        "backend.workers.state_age.get_session_maker", lambda: sessions_fixture,
    )
    monkeypatch.setattr(
        "backend.workers.state_age.get_settings",
        lambda: Settings(stuck_after_seconds=300),
    )
    assert await scan_stuck() == 3

    async with sessions_fixture() as session:
        async with session.begin():
            effect = await session.get(EffectReceipt, effect_id)
            effect.state = "NEEDS_REVIEW"
    async with sessions_fixture() as session:
        health = await queue_health(session, "alpha", 300)
        assert health["unresolved_effects"] == 0
        assert health["aged_unresolved_effects"] == 0
    assert await scan_stuck() == 2


@pytest.mark.asyncio
async def test_redteam_database_outage_rejects_admission_without_phantom_202(
    sessions_fixture, monkeypatch,
):
    from sqlalchemy.exc import SQLAlchemyError

    async def offline(*args, **kwargs):
        raise SQLAlchemyError("private connection string with credentials")

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    monkeypatch.setattr("backend.api.admit_incident", offline)
    app.dependency_overrides[get_settings] = lambda: Settings(
        local_token="admission-outage-test",
    )
    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/incidents",
                json={"target_id": "checkout"},
                headers={
                    "X-Tenant-Id": "alpha",
                    "X-OpsBlaze-Token": "admission-outage-test",
                    "Idempotency-Key": "outage-key",
                },
            )
            assert response.status_code == 503
            assert response.headers["retry-after"] == "30"
            assert "credentials" not in response.text
            assert "private" not in response.text
            assert "temporarily unavailable" in response.text
    finally:
        app.dependency_overrides.clear()

    async with sessions_fixture() as session:
        assert (await session.scalars(select(Incident))).all() == []
        assert (await session.scalars(select(OutboxEntry))).all() == []


@pytest.mark.asyncio
async def test_redteam_queue_ignores_forged_tenant_tag_on_foreign_effect(
    sessions_fixture,
):
    async with sessions_fixture() as session:
        foreign = await admit_incident(
            session, "beta", "foreign-effect",
            IncidentCreate(target_id="private-resource"),
        )
    effect_id = uuid4()
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(EffectReceipt(
                id=effect_id, tenant_id="alpha", incident_id=foreign.id,
                plan_digest="d" * 64, target_id="private-resource",
                adapter_id="simulator", idempotency_key=str(effect_id),
                fence=1, expected_version=1, state="UNKNOWN",
                attempt_count=1, updated_at=(
                    datetime.now(timezone.utc) - timedelta(days=1)
                ),
            ))
    async with sessions_fixture() as session:
        alpha = await queue_health(session, "alpha", 300)
        beta = await queue_health(session, "beta", 300)
        assert alpha["unresolved_effects"] == 0
        assert alpha["aged_unresolved_effects"] == 0
        assert beta["unresolved_effects"] == 0
        assert beta["aged_unresolved_effects"] == 0


@pytest.mark.asyncio
async def test_redteam_pending_approval_queue_age_and_tenant_scope(sessions_fixture):
    """P2-07: authoritative pending approval queue counts, exact p95, and tenant isolation."""
    from backend.models import IncidentPlan

    now = datetime.now(timezone.utc).replace(microsecond=0)
    alpha_ids = []
    for index in range(3):
        async with sessions_fixture() as session:
            inc = await admit_incident(
                session, "alpha", f"pend-app-{index}", IncidentCreate(target_id="checkout"),
            )
            alpha_ids.append(inc.id)
    async with sessions_fixture() as session:
        foreign = await admit_incident(
            session, "beta", "foreign-app", IncidentCreate(target_id="private"),
        )

    async with sessions_fixture() as session:
        async with session.begin():
            ages = [120, 600, 1800]
            for incident_id, age_sec in zip(alpha_ids, ages):
                row = await session.get(Incident, incident_id)
                row.status_projection = "AWAITING_APPROVAL"
                row.phase_entered_at = now - timedelta(seconds=age_sec)
                session.add(IncidentPlan(
                    id=uuid4(), tenant_id="alpha", incident_id=incident_id,
                    target_id="checkout", action_id="SIMULATOR_MARK_HEALTHY",
                    plan_digest="a" * 64, expected_version=1, evidence_ids=[],
                    status="PROPOSED", version=1,
                ))
            beta_row = await session.get(Incident, foreign.id)
            beta_row.status_projection = "AWAITING_APPROVAL"
            beta_row.phase_entered_at = now - timedelta(days=3)
            session.add(IncidentPlan(
                id=uuid4(), tenant_id="beta", incident_id=foreign.id,
                target_id="private", action_id="SIMULATOR_MARK_HEALTHY",
                plan_digest="b" * 64, expected_version=1, evidence_ids=[],
                status="PROPOSED", version=1,
            ))

    async with sessions_fixture() as session:
        alpha = await queue_health(session, "alpha", 300, now)
        beta = await queue_health(session, "beta", 300, now)
        assert alpha["pending_approvals"] == 3
        assert alpha["aged_pending_approvals"] == 2
        assert alpha["oldest_pending_approval_seconds"] == 1800
        assert alpha["pending_approval_age_p95_seconds"] == 1800
        assert beta["pending_approvals"] == 1
        assert beta["aged_pending_approvals"] == 1
        assert beta["oldest_pending_approval_seconds"] == 3 * 86400

    # Once decided, a plan leaves the pending approval queue immediately.
    async with sessions_fixture() as session:
        async with session.begin():
            plan = await session.scalar(
                select(IncidentPlan).where(IncidentPlan.incident_id == alpha_ids[2])
            )
            plan.status = "APPROVED"
    async with sessions_fixture() as session:
        alpha_after = await queue_health(session, "alpha", 300, now)
        assert alpha_after["pending_approvals"] == 2
        assert alpha_after["aged_pending_approvals"] == 1
        assert alpha_after["oldest_pending_approval_seconds"] == 600


@pytest.mark.asyncio
async def test_learning_health_metrics_are_aggregate_without_tenant_labels(
    sessions_fixture,
):
    from backend.operations import learning_health

    async with sessions_fixture() as session:
        report = await learning_health(session)
    assert report["tenant_labels_exported"] is False
    assert set(report) == {"tenant_labels_exported", "experience", "outcomes", "failures"}
    assert "pending_projections" in report["experience"]
    assert "dispute_rate" in report["outcomes"]
    assert "active_clusters" in report["failures"]

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(local_token="lh-token")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get(
                "/v1/operations/learning-health",
                headers={"X-Tenant-Id": "alpha", "X-OpsBlaze-Token": "lh-token"},
            )
            assert res.status_code == 200
            assert res.json()["tenant_labels_exported"] is False
    finally:
        app.dependency_overrides.clear()
