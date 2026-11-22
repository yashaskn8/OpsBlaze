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
                # Other PG integration tests leave unrelated tenants' intents.
                # Dispatch only the incident's tenant to avoid cross-test claims.
                assert await dispatch_once(env.client, sessions, tenant_filter="demo")

                async def await_state(expected: str) -> dict:
                    # Native Temporal startup can be slower under parallel CI
                    # security scans. Bound the wait and report observed state.
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + 30
                    observed = None
                    while loop.time() < deadline:
                        response = await client.get(
                            f"/v1/incidents/{incident_id}", headers=headers
                        )
                        assert response.status_code == 200, response.text
                        body = response.json()
                        observed = body["status_projection"]
                        if observed == expected:
                            return body
                        await asyncio.sleep(0.1)
                    # Include only workflow status/event types; no raw
                    # activity payloads, incident evidence or secrets.
                    from backend.workers.dispatcher import workflow_id
                    from uuid import UUID

                    handle = env.client.get_workflow_handle(
                        workflow_id("demo", UUID(incident_id))
                    )
                    try:
                        description = await handle.describe()
                        workflow_state = getattr(
                            description.status, "name", str(description.status)
                        )
                    except Exception:
                        workflow_state = "HISTORY_UNAVAILABLE"
                    raise AssertionError(
                        f"incident never reached {expected}; observed {observed}; "
                        f"Temporal workflow state: {workflow_state}"
                    )

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
                assert await dispatch_once(env.client, sessions, tenant_filter="demo")

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


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_failure_case_not_trusted_after_concurrent_outcome_dispute():
    """E3 case creation cannot outrun E2 invalidation without quarantine."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.learning.failures import FailureConflict, record_case
    from backend.models import FailureCase, FailureCluster, OutcomeRevision
    from backend.outcomes import dispute_outcome
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "failure-race-" + uuid4().hex[:12]
    try:
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant, "case", IncidentCreate(target_id="checkout"),
            )
        initial_id = uuid4()
        async with sessions() as session:
            async with session.begin():
                session.add(OutcomeRevision(
                    id=initial_id, tenant_id=tenant, incident_id=incident.id,
                    effect_id=None, revision=1, label="DISPUTED",
                    witness_source="SIMULATOR_WITNESS", observed_version=None,
                    reason="uncertain isolated simulator observation",
                ))

        async def classify_case():
            async with sessions() as session:
                try:
                    return await record_case(session, tenant, initial_id)
                except FailureConflict:
                    return None

        async def dispute_case():
            async with sessions() as session:
                return await dispute_outcome(
                    session, tenant, initial_id, 1,
                    "reviewer confirmed source evidence was superseded",
                )

        classified, disputed = await asyncio.gather(
            classify_case(), dispute_case(),
        )
        assert disputed.revision == 2
        async with sessions() as session:
            cases = (await session.scalars(
                select(FailureCase).where(FailureCase.incident_id == incident.id)
            )).all()
            if classified is None:
                assert cases == []
            else:
                assert len(cases) == 1
                assert cases[0].review_status == "STALE"
                cluster = await session.get(FailureCluster, cases[0].cluster_id)
                assert cluster.quarantined
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_failure_review_serializes_with_outcome_dispute():
    """E3 reviewer cannot leave a corrected case trusted after an E2 reversal."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.learning.failures import (
        FailureConflict, record_case, review_classification,
    )
    from backend.models import (
        EffectReceipt, FailureCase, FailureCluster, OutcomeRevision,
    )
    from backend.outcomes import dispute_outcome
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "review-race-" + uuid4().hex[:12]
    outcome_id = uuid4()
    try:
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant, "review", IncidentCreate(target_id="checkout"),
            )
        async with sessions() as session:
            async with session.begin():
                session.add(EffectReceipt(
                    id=uuid4(), tenant_id=tenant, incident_id=incident.id,
                    plan_digest="f" * 64, target_id="checkout",
                    adapter_id="simulator", idempotency_key="review",
                    fence=1, expected_version=1, state="UNKNOWN",
                    attempt_count=1,
                ))
                session.add(OutcomeRevision(
                    id=outcome_id, tenant_id=tenant, incident_id=incident.id,
                    effect_id=None, revision=1, label="DISPUTED",
                    witness_source="SIMULATOR_WITNESS", observed_version=None,
                    reason="unknown outcome with independently disputed status",
                ))
        async with sessions() as session:
            original = await record_case(session, tenant, outcome_id)
            assert original.origin_code == "OUTCOME_DISPUTED"
            assert original.classification_version == 1

        async def review():
            async with sessions() as session:
                try:
                    return await review_classification(
                        session, tenant, original.id, 1,
                        "EFFECT_UNKNOWN", None,
                        "reviewed the unresolved simulator effect receipt",
                    )
                except FailureConflict:
                    return None

        async def reverse():
            async with sessions() as session:
                return await dispute_outcome(
                    session, tenant, outcome_id, 1,
                    "new independent feedback invalidates prior classification",
                )

        correction, revision = await asyncio.gather(review(), reverse())
        assert revision.revision == 2
        async with sessions() as session:
            stored = await session.get(FailureCase, original.id)
            assert stored.review_status == "STALE"
            cluster = await session.get(FailureCluster, stored.cluster_id)
            assert cluster is not None and cluster.quarantined
            if correction is None:
                assert stored.classification_version == 1
            else:
                assert stored.classification_version == 2
                assert stored.origin_code == "EFFECT_UNKNOWN"
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_parallel_dispatchers_do_not_reorder_same_incident_signals():
    """B1: two claimers may handle different incidents but not a later signal."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.models import OutboxEntry
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident
    from backend.workers.dispatcher import claim_intent

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "ordered-outbox-" + uuid4().hex[:12]
    try:
        async with sessions() as session:
            first = await admit_incident(
                session, tenant, "one", IncidentCreate(target_id="checkout"),
            )
        async with sessions() as session:
            second = await admit_incident(
                session, tenant, "two", IncidentCreate(target_id="checkout"),
            )
        async with sessions() as session:
            async with session.begin():
                starts = (await session.scalars(
                    select(OutboxEntry).where(
                        OutboxEntry.incident_id.in_([first.id, second.id])
                    )
                )).all()
                for row in starts:
                    row.delivered_at = datetime.now(timezone.utc)
                first_signal = OutboxEntry(
                    incident_id=first.id, kind="ACKNOWLEDGE_REVIEW",
                )
                session.add(first_signal)
                await session.flush()
                second_signal = OutboxEntry(
                    incident_id=first.id, kind="CANCEL_WORKFLOW",
                )
                other_signal = OutboxEntry(
                    incident_id=second.id, kind="CANCEL_WORKFLOW",
                )
                session.add_all([second_signal, other_signal])
                await session.flush()
                first_id, second_id, other_id = (
                    first_signal.id, second_signal.id, other_signal.id,
                )

        claimed = await asyncio.gather(
            claim_intent(sessions, tenant), claim_intent(sessions, tenant),
        )
        assert {item[0] for item in claimed if item} == {first_id, other_id}
        assert second_id not in [item[0] for item in claimed if item]
        assert await claim_intent(sessions, tenant) is None

        async with sessions() as session:
            async with session.begin():
                (await session.get(OutboxEntry, first_id)).delivered_at = (
                    datetime.now(timezone.utc)
                )
        new_claim = await claim_intent(sessions, tenant)
        assert new_claim is not None and new_claim[0] == second_id
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_parallel_effect_preparation_creates_one_immutable_receipt():
    """Two workers can race the absent-row boundary without duplicate effect IDs."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.adapters.simulator import Permit
    from backend.effects.gateway import EffectConflict, prepare_receipt
    from backend.models import EffectReceipt
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "receipt-race-" + uuid4().hex[:12]
    try:
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant, "prepare", IncidentCreate(target_id="checkout"),
            )
        permit = Permit(
            tenant_id=tenant, incident_id=incident.id,
            target_id="checkout", action_digest="b" * 64,
            fence=1, expected_version=1,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        ids = await asyncio.gather(*(
            prepare_receipt(sessions, permit) for _ in range(10)
        ))
        assert len(set(ids)) == 1
        async with sessions() as session:
            receipts = (await session.scalars(
                select(EffectReceipt).where(
                    EffectReceipt.incident_id == incident.id
                )
            )).all()
            assert len(receipts) == 1
            assert receipts[0].attempt_count == 0
            assert receipts[0].state == "PREPARED"

        altered = Permit(
            **{**permit.__dict__, "expected_version": 2},
        )
        try:
            await prepare_receipt(sessions, altered)
            raise AssertionError("same logical effect accepted different permit")
        except EffectConflict:
            pass
        async with sessions() as session:
            remaining = await session.get(EffectReceipt, ids[0])
            assert remaining.expected_version == 1
            assert remaining.state == "PREPARED"
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_postgres_racing_sent_checkpoint_only_calls_adapter_once(monkeypatch):
    """B3: row locks serialize duplicate dispatch after both see PREPARED."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.adapters.simulator import SimulatorAdapter
    from backend.effects import gateway
    from backend.effects.reservations import reserve_simulator_target
    from backend.models import (
        EffectReceipt, ResourceEpoch, SimulatorAppliedEffect, SimulatorTarget,
    )
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "dispatch-race-" + uuid4().hex[:12]
    entering = 0
    reached = asyncio.Event()
    sent_state = gateway.set_state

    async def release_together(sessions_arg, effect_id, state, *args, **kwargs):
        nonlocal entering
        if state == "SENT":
            entering += 1
            if entering == 2:
                reached.set()
            await asyncio.wait_for(reached.wait(), timeout=10)
        return await sent_state(sessions_arg, effect_id, state, *args, **kwargs)

    monkeypatch.setattr(gateway, "set_state", release_together)
    calls = 0

    class CountedSimulator(SimulatorAdapter):
        async def apply(self, effect_id, permit):
            nonlocal calls
            calls += 1
            return await super().apply(effect_id, permit)

    try:
        async with sessions() as session:
            async with session.begin():
                session.add(SimulatorTarget(
                    tenant_id=tenant, target_id="checkout",
                    version=1, healthy=False, last_fence=0,
                ))
                session.add(ResourceEpoch(
                    tenant_id=tenant, target_id="checkout",
                    latest_fence=0, version_seen=1,
                ))
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant, "dispatch",
                IncidentCreate(target_id="checkout"),
            )
        permit = await reserve_simulator_target(
            sessions, tenant, "checkout", incident.id, "9" * 64,
        )
        adapter = CountedSimulator(sessions)
        outcomes = await asyncio.gather(*(
            gateway.execute_simulator_effect(sessions, adapter, permit)
            for _ in range(2)
        ))
        assert "VERIFIED" in outcomes
        assert set(outcomes).issubset({"UNKNOWN", "VERIFIED"})
        assert calls == 1
        async with sessions() as session:
            receipt = await session.scalar(
                select(EffectReceipt).where(
                    EffectReceipt.incident_id == incident.id,
                )
            )
            applied = (await session.scalars(select(SimulatorAppliedEffect).where(
                SimulatorAppliedEffect.tenant_id == tenant,
            ))).all()
            assert receipt.state == "VERIFIED"
            assert receipt.attempt_count == 1
            assert len(applied) == 1
        assert await adapter.observe(tenant, "checkout") == (2, True)
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_lesson_validation_cannot_outlive_concurrent_outcome_reversal():
    """E4 approval is either rejected or transactionally revoked after E2 correction."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.experiences import capture_capsule, set_consent
    from backend.memory.lessons import LessonConflict, create_draft, validate_draft
    from backend.models import OperationalLesson, OutcomeRevision
    from backend.outcomes import dispute_outcome
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "lesson-race-" + uuid4().hex[:12]
    source_id = uuid4()
    try:
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant, "lesson",
                IncidentCreate(target_id="checkout"),
            )
        async with sessions() as session:
            await set_consent(session, tenant, incident.id, 0, True)
        async with sessions() as session:
            async with session.begin():
                session.add(OutcomeRevision(
                    id=source_id, tenant_id=tenant, incident_id=incident.id,
                    effect_id=None, revision=1, label="VERIFIED",
                    witness_source="SIMULATOR_WITNESS",
                    observed_version=1, reason="isolated valid source fixture",
                ))
        async with sessions() as session:
            capsule = await capture_capsule(session, tenant, incident.id)
        async with sessions() as session:
            lesson = await create_draft(
                session, tenant, capsule.id, "CHECK_VERSION",
            )

        async def validate():
            async with sessions() as session:
                try:
                    return await validate_draft(
                        session, tenant, lesson.id, 1,
                    )
                except LessonConflict:
                    return None

        async def reverse():
            async with sessions() as session:
                return await dispute_outcome(
                    session, tenant, source_id, 1,
                    "independent source contradicted validated memory",
                )

        validated, disputed = await asyncio.gather(validate(), reverse())
        assert disputed.revision == 2
        async with sessions() as session:
            stored = await session.get(OperationalLesson, lesson.id)
            assert stored.status != "VALIDATED"
            assert stored.status != "ACTIVE"
            if validated is not None:
                assert stored.status == "REVOKED"
                assert stored.version == 3
            else:
                assert stored.status in {"DRAFT", "REVOKED"}
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_parallel_postgres_probe_retries_reserve_one_budget_slot() -> None:
    """A2: row-locked retries cannot create duplicate tool observations."""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.models import Evidence, Incident, ProbeCall
    from backend.probes.gateway import invoke_probe
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "probe-race-" + uuid4().hex[:12]
    try:
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant, "stable-probe-key",
                IncidentCreate(target_id="unseeded-simulator"),
            )

        async def call_probe():
            return await invoke_probe(
                sessions, tenant, incident.id, 1, "simulator.health",
            )

        results = await asyncio.gather(*(call_probe() for _ in range(8)))
        assert {result.outcome for result in results} == {"UNAVAILABLE"}
        assert len({result.evidence_id for result in results}) == 1

        async with sessions() as session:
            persisted = await session.get(Incident, incident.id)
            assert persisted.probe_used == 1
            assert await session.scalar(
                select(func.count()).select_from(ProbeCall).where(
                    ProbeCall.incident_id == incident.id,
                )
            ) == 1
            assert await session.scalar(
                select(func.count()).select_from(Evidence).where(
                    Evidence.incident_id == incident.id,
                )
            ) == 1
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_idp_event_replay_concurrency_revokes_once_without_duplicated_audit():
    """Adversarial duplicate deliveries across independent PG sessions."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.models import (
        ApprovalGrant, ApprovalRevocation, IdentityProviderEvent,
        IncidentPlan, RevokedIdentity,
    )
    from backend.schemas import IncidentCreate
    from backend.security.revocation import revoke_identity
    from backend.services import admit_incident

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "idp-replay-" + uuid4().hex[:10]
    reviewer = "oidc:" + "a" * 32
    admin = "oidc:" + "b" * 32
    event_digest = uuid4().hex + uuid4().hex
    plan_id, approval_id = uuid4(), uuid4()
    try:
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant, "revocation-replay",
                IncidentCreate(target_id="checkout"),
            )
        async with sessions() as session:
            async with session.begin():
                session.add(IncidentPlan(
                    id=plan_id, tenant_id=tenant, incident_id=incident.id,
                    target_id="checkout", action_id="SIMULATOR_MARK_HEALTHY",
                    plan_digest="a" * 64, expected_version=1,
                    evidence_ids=[], status="APPROVED", version=2,
                    decided_by=reviewer, review_epoch=1,
                ))
                await session.flush()
                session.add(ApprovalGrant(
                    id=approval_id, tenant_id=tenant, plan_id=plan_id,
                    plan_digest="a" * 64, reviewer_id=reviewer,
                    scope="SIMULATOR_MARK_HEALTHY", decision="APPROVE",
                    state="APPROVED", version=1,
                    reason="approved local concurrency fixture",
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                ))

        async def deliver_same_event():
            async with sessions() as session:
                _, count = await revoke_identity(
                    session, tenant, reviewer, admin,
                    "identity provider signed revocation fixture",
                    event_digest=event_digest,
                )
                return count

        results = await asyncio.gather(*(deliver_same_event() for _ in range(8)))
        assert sorted(results) == [0] * 7 + [1]
        async with sessions() as session:
            events = (await session.scalars(
                select(IdentityProviderEvent).where(
                    IdentityProviderEvent.tenant_id == tenant,
                )
            )).all()
            audits = (await session.scalars(
                select(ApprovalRevocation).where(
                    ApprovalRevocation.approval_id == approval_id,
                )
            )).all()
            assert len(events) == 1 and events[0].event_digest == event_digest
            assert len(audits) == 1
            assert (await session.get(ApprovalGrant, approval_id)).state == "REVOKED"
            assert (await session.get(IncidentPlan, plan_id)).status == "REVOKED"
            assert await session.get(RevokedIdentity, (tenant, reviewer)) is not None
    finally:
        await engine.dispose()
