from datetime import datetime, timedelta, timezone

"""Native PostgreSQL + Temporal vertical slice; no Docker, no production effects."""

import asyncio
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.api import app
from backend.adapters.conformance import execute_suite
from backend.database import get_session_maker
from backend.settings import get_settings
from backend.workers.dispatcher import dispatch_once
from backend.workers.seed_simulator import seed
from backend.workflows.activities import (
    apply_approved_simulator_plan,
    inspect_incident_read_only,
    project_incident_status,
    select_incident_route,
)
from backend.workflows.incident import IncidentWorkflow


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_postgres_temporal_simulator_recovery() -> None:
    settings = get_settings()
    assert settings.database_url.startswith("postgresql+asyncpg:")
    await seed("demo", "checkout")
    sessions = get_session_maker()
    assert (await execute_suite(sessions)).state == "LAB_PASS"

    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue=settings.temporal_task_queue,
            workflows=[IncidentWorkflow],
            activities=[
                inspect_incident_read_only,
                project_incident_status,
                apply_approved_simulator_plan,
                select_incident_route,
            ],
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {
                    "X-Tenant-Id": "demo",
                    "X-OpsBlaze-Token": settings.local_token,
                }
                created = await client.post(
                    "/v1/incidents",
                    headers={**headers, "Idempotency-Key": str(uuid4())},
                    json={"target_id": "checkout"},
                )
                assert created.status_code == 202, created.text
                incident_id = created.json()["id"]
                assert await dispatch_once(env.client, sessions)

                async def await_state(expected: str) -> dict:
                    for _ in range(100):
                        response = await client.get(
                            f"/v1/incidents/{incident_id}", headers=headers
                        )
                        assert response.status_code == 200, response.text
                        body = response.json()
                        if body["status_projection"] == expected:
                            return body
                        await asyncio.sleep(0.1)
                    raise AssertionError(f"incident never reached {expected}")

                await await_state("AWAITING_APPROVAL")
                evidence = await client.get(
                    f"/v1/incidents/{incident_id}/evidence", headers=headers
                )
                assert evidence.status_code == 200
                assert len(evidence.json()) == 2

                plans = await client.get(
                    f"/v1/incidents/{incident_id}/plans", headers=headers
                )
                assert plans.status_code == 200 and len(plans.json()) == 1
                plan = plans.json()[0]
                assert plan["status"] == "PROPOSED"
                decision = await client.post(
                    f"/v1/plans/{plan['id']}/decisions",
                    headers={
                        **headers,
                        "If-Match": str(plan["version"]),
                        "X-OpsBlaze-Reviewer-Token": settings.review_token,
                    },
                    json={"decision": "APPROVE"},
                )
                assert decision.status_code == 201, decision.text
                assert await dispatch_once(env.client, sessions)

                final = await await_state("VERIFIED")
                assert final["status_projection"] == "VERIFIED"
                effects = await client.get(
                    f"/v1/incidents/{incident_id}/effects", headers=headers
                )
                assert effects.status_code == 200 and len(effects.json()) == 1
                assert effects.json()[0]["state"] == "VERIFIED"
                target = await client.get(
                    "/v1/targets/checkout/simulator", headers=headers
                )
                assert target.status_code == 200
                assert target.json()["healthy"] is True
                assert target.json()["version"] == 2


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_parallel_postgres_admission_is_one_logical_incident() -> None:
    """One incident, intent, event and quota slot for concurrent retries."""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.models import AdmissionUsage, Incident, OutboxEntry, PolicyAssignment, UIEvent
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    settings = get_settings()
    assert settings.database_url.startswith("postgresql+asyncpg:")
    tenant = "concurrent-" + uuid4().hex[:12]
    # A dedicated engine is owned by this test and disposed on its event loop.
    # The other integration test's cached app engine belongs to a prior loop.
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        payload = IncidentCreate(target_id="checkout")

        async def admit():
            async with sessions() as session:
                return await admit_incident(session, tenant, "stable-key", payload)

        admitted = await asyncio.gather(*(admit() for _ in range(8)))
        ids = {item.id for item in admitted}
        assert len(ids) == 1
        incident_id = next(iter(ids))

        async with sessions() as session:
            count = lambda model: select(func.count()).select_from(model)
            assert await session.scalar(
                count(Incident).where(Incident.tenant_id == tenant)
            ) == 1
            assert await session.scalar(
                count(OutboxEntry).where(OutboxEntry.incident_id == incident_id)
            ) == 1
            assert await session.scalar(
                count(UIEvent).where(UIEvent.incident_id == incident_id)
            ) == 1
            assert await session.scalar(
                count(PolicyAssignment).where(
                    PolicyAssignment.tenant_id == tenant,
                    PolicyAssignment.incident_id == incident_id,
                )
            ) == 1
            assert await session.scalar(
                select(func.sum(AdmissionUsage.accepted)).where(
                    AdmissionUsage.tenant_id == tenant
                )
            ) == 1
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_active_capacity_is_atomic_under_parallel_admissions() -> None:
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.models import Incident
    from backend.schemas import IncidentCreate
    from backend.services import RateLimited, admit_incident
    from backend.settings import Settings

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "cap-" + uuid4().hex[:12]

    # A local settings override applies only to service capacity accounting,
    # not the database URL, and is restored before returning.
    import backend.services as services
    original_settings = services.get_settings
    services.get_settings = lambda: Settings(
        max_active_incidents_per_tenant=2,
        max_active_incidents_global=100,
    )
    try:
        async def attempt(index: int) -> bool:
            async with sessions() as session:
                try:
                    await admit_incident(
                        session, tenant, f"key-{index}",
                        IncidentCreate(target_id=f"fixture-{index}"),
                    )
                    return True
                except RateLimited:
                    return False

        accepted = await asyncio.gather(*(attempt(i) for i in range(6)))
        assert sum(accepted) == 2
        async with sessions() as session:
            assert await session.scalar(
                select(func.count()).select_from(Incident)
                .where(Incident.tenant_id == tenant)
            ) == 2
    finally:
        services.get_settings = original_settings
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_parallel_target_reservations_are_epoch_fenced() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.adapters.simulator import SimulatorAdapter, StaleFence
    from backend.effects.reservations import (
        ReservationBusy, reserve_simulator_target,
    )
    from backend.models import ResourceEpoch, ResourceReservation, SimulatorTarget
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "fence-" + uuid4().hex[:12]
    target_id = "shared-fixture"
    try:
        async with sessions() as session:
            async with session.begin():
                session.add(SimulatorTarget(
                    tenant_id=tenant, target_id=target_id,
                    version=1, healthy=False, last_fence=0,
                ))
                session.add(ResourceEpoch(
                    tenant_id=tenant, target_id=target_id,
                    latest_fence=0, version_seen=1,
                ))
        ids = []
        for i in range(5):
            async with sessions() as session:
                incident = await admit_incident(
                    session, tenant, f"fence-{i}",
                    IncidentCreate(target_id=target_id),
                )
                ids.append(incident.id)

        async def attempt(index: int):
            try:
                permit = await reserve_simulator_target(
                    sessions, tenant, target_id, ids[index],
                    f"{index + 1:064x}",
                )
                return (index, permit)
            except ReservationBusy:
                return (index, None)

        results = await asyncio.gather(*(attempt(i) for i in range(5)))
        winners = [(i, permit) for i, permit in results if permit is not None]
        assert len(winners) == 1
        winner_index, original = winners[0]
        assert original.fence == 1

        async with sessions() as session:
            reservation = await session.get(
                ResourceReservation, (tenant, target_id)
            )
            assert reservation.holder_incident == ids[winner_index]
            assert reservation.fence == original.fence

        async with sessions() as session:
            async with session.begin():
                reservation = await session.get(
                    ResourceReservation, (tenant, target_id), with_for_update=True
                )
                reservation.expires_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                )

        successor = (winner_index + 1) % len(ids)
        newer = await reserve_simulator_target(
            sessions, tenant, target_id, ids[successor],
            f"{successor + 1:064x}",
        )
        assert newer.fence > original.fence
        adapter = SimulatorAdapter(sessions)
        with pytest.raises(StaleFence):
            await adapter.apply(uuid4(), original)
        assert await adapter.observe(tenant, target_id) == (1, False)
    finally:
        await engine.dispose()

@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_postgres_approval_revoke_and_dispatch_race_is_serialized(monkeypatch):
    """Either one effect crosses the boundary or the approval is revoked."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.effects.gateway import EffectConflict, set_state
    from backend.models import (
        ApprovalGrant, ApprovalRevocation, EffectReceipt, IncidentPlan,
    )
    from backend.plans import PlanConflict, revoke_approval
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    async def valid_simulator_evidence(*_args):
        # This test isolates the approval lock-order; separate suites verify
        # evidence fingerprints, target versions and conformance.
        return True

    monkeypatch.setattr(
        "backend.plans.current_plan_evidence_is_intact",
        valid_simulator_evidence,
    )
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = "approval-race-" + uuid4().hex[:10]
    plan_id, grant_id, receipt_id = uuid4(), uuid4(), uuid4()
    digest = "a" * 64
    try:
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant_id, "one",
                IncidentCreate(target_id="isolated-simulator"),
            )
        async with sessions() as session:
            async with session.begin():
                session.add(IncidentPlan(
                    id=plan_id, tenant_id=tenant_id, incident_id=incident.id,
                    target_id="isolated-simulator",
                    action_id="SIMULATOR_MARK_HEALTHY",
                    plan_digest=digest, expected_version=1,
                    evidence_ids=[], status="APPROVED", version=2,
                    decided_by="reviewer-2", review_epoch=get_settings().review_epoch,
                ))
                # Flush the referenced plan before inserting the grant; these
                # objects have no ORM relationship to order the SQL statements.
                await session.flush()
                session.add(ApprovalGrant(
                    id=grant_id, tenant_id=tenant_id, plan_id=plan_id,
                    plan_digest=digest, reviewer_id="reviewer-2",
                    scope="SIMULATOR_MARK_HEALTHY", decision="APPROVE",
                    state="APPROVED", reason="reviewed exact fixture",
                    version=1,
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
                ))
                session.add(EffectReceipt(
                    id=receipt_id, tenant_id=tenant_id,
                    incident_id=incident.id, plan_digest=digest,
                    target_id="isolated-simulator", adapter_id="simulator",
                    idempotency_key=str(receipt_id), fence=1,
                    expected_version=1, state="PREPARED", attempt_count=0,
                ))

        async def dispatch():
            return await set_state(
                sessions, receipt_id, "SENT", plan_id=plan_id,
            )

        async def revoke():
            async with sessions() as session:
                return await revoke_approval(
                    session, tenant_id, grant_id, 1, "reviewer-3",
                    "revoked just before effect dispatch",
                )

        outcomes = await asyncio.gather(
            dispatch(), revoke(), return_exceptions=True,
        )
        conflicts = [outcome for outcome in outcomes if isinstance(
            outcome, (EffectConflict, PlanConflict)
        )]
        assert len(conflicts) == 1, outcomes
        async with sessions() as session:
            grant = await session.get(ApprovalGrant, grant_id)
            receipt = await session.get(EffectReceipt, receipt_id)
            if grant.state == "CONSUMED":
                assert receipt.state == "SENT" and receipt.attempt_count == 1
                assert isinstance(outcomes[1], PlanConflict)
                assert grant.consumed_at is not None
            else:
                assert grant.state == "REVOKED"
                assert receipt.state == "PREPARED" and receipt.attempt_count == 0
                assert isinstance(outcomes[0], EffectConflict)
                assert (await session.get(IncidentPlan, plan_id)).status == "REVOKED"
                audit = await session.scalar(
                    select(ApprovalRevocation).where(
                        ApprovalRevocation.approval_id == grant_id
                    )
                )
                assert audit.revoked_by == "reviewer-3"
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_parallel_outcome_witnesses_and_disputes_serialize_by_incident() -> None:
    """Only one revision per change, even across concurrent PostgreSQL sessions."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.adapters.simulator import SimulatorAdapter
    from backend.effects.gateway import execute_simulator_effect
    from backend.effects.reservations import reserve_simulator_target
    from backend.models import (
        EffectReceipt, OutcomeRevision, ResourceEpoch, SimulatorTarget,
    )
    from backend.outcomes import (
        OutcomeConflict, adjudicate_simulator, dispute_outcome,
    )
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = "outcome-race-" + uuid4().hex[:12]
    try:
        async with sessions() as session:
            async with session.begin():
                session.add(SimulatorTarget(
                    tenant_id=tenant_id, target_id="checkout",
                    version=1, healthy=False, last_fence=0,
                ))
                session.add(ResourceEpoch(
                    tenant_id=tenant_id, target_id="checkout",
                    latest_fence=0, version_seen=1,
                ))
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant_id, "witness", IncidentCreate(target_id="checkout"),
            )
        permit = await reserve_simulator_target(
            sessions, tenant_id, "checkout", incident.id, "a" * 64,
        )
        adapter = SimulatorAdapter(sessions)
        assert await execute_simulator_effect(
            sessions, adapter, permit,
        ) == "VERIFIED"
        async with sessions() as session:
            receipt = await session.scalar(
                select(EffectReceipt).where(EffectReceipt.incident_id == incident.id)
            )
            effect_id = receipt.id

        results = await asyncio.gather(*(
            adjudicate_simulator(
                sessions, adapter, tenant_id, incident.id, effect_id,
            )
            for _ in range(6)
        ))
        assert results == ["VERIFIED"] * 6

        async with sessions() as session:
            outcome = await session.scalar(
                select(OutcomeRevision).where(
                    OutcomeRevision.incident_id == incident.id
                )
            )
            assert outcome.revision == 1
            outcome_id = outcome.id

        async def dispute(index: int) -> str:
            async with sessions() as session:
                try:
                    row = await dispute_outcome(
                        session, tenant_id, outcome_id, 1,
                        f"reviewer {index} found contradictory independent evidence",
                    )
                    assert row is not None
                    return "DISPUTED"
                except OutcomeConflict:
                    return "STALE"

        disputes = await asyncio.gather(*(dispute(i) for i in range(6)))
        assert disputes.count("DISPUTED") == 1
        assert disputes.count("STALE") == 5
        assert await adjudicate_simulator(
            sessions, adapter, tenant_id, incident.id, effect_id,
        ) == "DISPUTED"

        async with sessions() as session:
            rows = (await session.scalars(
                select(OutcomeRevision)
                .where(OutcomeRevision.incident_id == incident.id)
                .order_by(OutcomeRevision.revision)
            )).all()
            assert [(row.revision, row.witness_source) for row in rows] == [
                (1, "SIMULATOR_WITNESS"), (2, "OPERATOR_DISPUTE"),
            ]
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_capture_cannot_remain_eligible_when_outcome_disputed_concurrently():
    """E1 capture and E2 reversal serialize on the same PostgreSQL parent row."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.experiences import (
        ConsentConflict, capture_capsule, list_capsules, set_consent,
    )
    from backend.models import ExperienceCapsule, OutcomeRevision
    from backend.outcomes import dispute_outcome
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "capture-race-" + uuid4().hex[:12]
    try:
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant, "capsule", IncidentCreate(target_id="checkout"),
            )
        async with sessions() as session:
            await set_consent(session, tenant, incident.id, 0, True)
        async with sessions() as session:
            async with session.begin():
                initial = OutcomeRevision(
                    id=uuid4(), tenant_id=tenant, incident_id=incident.id,
                    effect_id=None, revision=1, label="VERIFIED",
                    witness_source="SIMULATOR_WITNESS", observed_version=1,
                    reason="isolated database concurrency fixture",
                )
                session.add(initial)

        async def capture():
            async with sessions() as session:
                try:
                    return await capture_capsule(session, tenant, incident.id)
                except ConsentConflict:
                    return None

        async def dispute():
            async with sessions() as session:
                return await dispute_outcome(
                    session, tenant, initial.id, 1,
                    "independent reviewer disputes the simulated witness",
                )

        captured, disputed = await asyncio.gather(capture(), dispute())
        assert disputed.label == "DISPUTED"
        async with sessions() as session:
            assert await list_capsules(session, tenant) == []
            rows = (await session.scalars(
                select(ExperienceCapsule).where(
                    ExperienceCapsule.incident_id == incident.id,
                )
            )).all()
            if captured is None:
                assert rows == []
            else:
                assert len(rows) == 1
                assert rows[0].status == "QUARANTINED"
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_security_revocation_serializes_with_new_reviewer_grant(monkeypatch):
    """A grant cannot survive a completed concurrent reviewer revocation."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.models import (
        ApprovalGrant, Incident, IncidentPlan, RevokedIdentity,
    )
    from backend.plans import PlanConflict, decide_plan
    from backend.schemas import IncidentCreate
    from backend.security.revocation import revoke_identity
    from backend.services import admit_incident

    async def intact(*_args):
        return True

    monkeypatch.setattr("backend.plans.current_plan_evidence_is_intact", intact)
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "revocation-race-" + uuid4().hex[:10]
    reviewer = "oidc:" + "a" * 32
    security = "oidc:" + "b" * 32
    plan_id = uuid4()
    try:
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant, "revoke",
                IncidentCreate(target_id="isolated-simulator"),
            )
        async with sessions() as session:
            async with session.begin():
                original = await session.get(Incident, incident.id)
                original.status_projection = "AWAITING_APPROVAL"
                session.add(IncidentPlan(
                    id=plan_id, tenant_id=tenant, incident_id=incident.id,
                    target_id="isolated-simulator",
                    action_id="SIMULATOR_MARK_HEALTHY",
                    plan_digest="d" * 64, expected_version=1, evidence_ids=[],
                    status="PROPOSED", version=1,
                ))

        async def grant():
            async with sessions() as session:
                try:
                    await decide_plan(
                        session, tenant, plan_id, 1, "APPROVE",
                        decided_by=reviewer,
                    )
                    return "APPROVED"
                except PlanConflict:
                    return "DENIED"

        async def security_revoke():
            async with sessions() as session:
                _identity, count = await revoke_identity(
                    session, tenant, reviewer, security,
                    "security owner revoked reviewer before effect dispatch",
                )
                return count

        grant_outcome, affected = await asyncio.gather(
            grant(), security_revoke(),
        )
        assert grant_outcome in {"APPROVED", "DENIED"}
        assert affected in {0, 1}
        async with sessions() as session:
            revoked = await session.get(RevokedIdentity, (tenant, reviewer))
            assert revoked is not None and revoked.revoked_by == security
            grant_row = await session.scalar(
                select(ApprovalGrant).where(ApprovalGrant.plan_id == plan_id)
            )
            assert grant_row is None or grant_row.state == "REVOKED"
            state = await session.get(IncidentPlan, plan_id)
            assert state.status in {"PROPOSED", "REVOKED"}
            assert state.status != "APPROVED"
    finally:
        await engine.dispose()
