"""B1 discrepancy scan never guesses workflow state or mutates incidents."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.adapters.simulator import SimulatorAdapter
from backend.effects.gateway import execute_simulator_effect, prepare_receipt
from backend.effects.reservations import reserve_simulator_target
from backend.models import EffectReceipt, Incident, OutboxEntry, ResourceEpoch, SimulatorTarget
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.workers.dispatcher import dispatch_batch
from backend.workers.outbox import acknowledge_claim, claim_intent_batch
from backend.workers.reconciler import compare_temporal_projection, reconcile_unresolved_effects


class FakeWorkflow:
    def __init__(self, status=None, error=None):
        self.status = status
        self.error = error

    async def describe(self):
        if self.error:
            raise self.error
        return type("Description", (), {"status": self.status})()


class FakeTemporal:
    def __init__(self, status=None, error=None):
        self.status = status
        self.error = error

    def get_workflow_handle(self, workflow_id):
        return FakeWorkflow(self.status, self.error)


@pytest.mark.asyncio
async def test_aged_temporal_projection_report_is_read_only(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", "reconcile", IncidentCreate(target_id="checkout")
        )
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    async with sessions_fixture() as session:
        async with session.begin():
            current = await session.get(Incident, incident.id)
            current.status_projection = "EXECUTING"
            current.phase_entered_at = old
            start = await session.scalar(
                select(OutboxEntry).where(
                    OutboxEntry.incident_id == incident.id,
                    OutboxEntry.kind == "START_WORKFLOW",
                )
            )
            start.delivered_at = old

    completed = await compare_temporal_projection(
        sessions_fixture,
        FakeTemporal(status=type("Status", (), {"name": "COMPLETED"})()),
        stale_seconds=30,
    )
    assert len(completed) == 1
    assert completed[0]["reason"] == "PROJECTION_LAGS_TERMINAL_HISTORY"
    assert completed[0]["safe_action"] == "INSPECT_ONLY"

    unknown = await compare_temporal_projection(
        sessions_fixture, FakeTemporal(error=ConnectionError("offline")),
        stale_seconds=30,
    )
    assert unknown[0]["reason"] == "TEMPORAL_HISTORY_UNAVAILABLE_OR_UNKNOWN"
    assert unknown[0]["temporal_status"] == "UNKNOWN"

    async with sessions_fixture() as session:
        still = await session.get(Incident, incident.id)
        assert still.status_projection == "EXECUTING"


@pytest.mark.asyncio
async def test_human_approval_wait_is_not_misdiagnosed_as_worker_stall(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", "human", IncidentCreate(target_id="checkout")
        )
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    async with sessions_fixture() as session:
        async with session.begin():
            current = await session.get(Incident, incident.id)
            current.status_projection = "AWAITING_APPROVAL"
            current.phase_entered_at = old
            start = await session.scalar(
                select(OutboxEntry).where(OutboxEntry.incident_id == incident.id)
            )
            start.delivered_at = old

    assert await compare_temporal_projection(
        sessions_fixture, FakeTemporal(status="RUNNING"), stale_seconds=30,
    ) == []


@pytest.mark.asyncio
async def test_bounded_effect_reconciliation_poller_handles_lost_ack_and_outage(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(tenant_id="t", target_id="svc-a", version=1, healthy=False, last_fence=0))
            session.add(SimulatorTarget(tenant_id="t", target_id="svc-b", version=1, healthy=False, last_fence=0))
            session.add(ResourceEpoch(tenant_id="t", target_id="svc-a", latest_fence=0, version_seen=1))
            session.add(ResourceEpoch(tenant_id="t", target_id="svc-b", latest_fence=0, version_seen=1))
        inc_a = await admit_incident(session, "t", "rec-a", IncidentCreate(target_id="svc-a"))
    async with sessions_fixture() as session:
        inc_b = await admit_incident(session, "t", "rec-b", IncidentCreate(target_id="svc-b"))

    permit_a = await reserve_simulator_target(sessions_fixture, "t", "svc-a", inc_a.id, "a" * 64)
    permit_b = await reserve_simulator_target(sessions_fixture, "t", "svc-b", inc_b.id, "b" * 64)

    class LostAckAdapter(SimulatorAdapter):
        async def apply(self, effect_id, permit):
            await super().apply(effect_id, permit)
            raise TimeoutError("lost ack after commit")

    class StatusOutageAdapter(SimulatorAdapter):
        async def query_effect(self, effect_id):
            raise ConnectionError("status lookup unavailable")

    lost_adapter = LostAckAdapter(sessions_fixture)
    eid_a = await prepare_receipt(sessions_fixture, permit_a)
    eid_b = await prepare_receipt(sessions_fixture, permit_b)
    assert await execute_simulator_effect(sessions_fixture, lost_adapter, permit_a) == "UNKNOWN"
    assert await execute_simulator_effect(sessions_fixture, lost_adapter, permit_b) == "UNKNOWN"

    # Outage keeps receipts UNKNOWN with attempt_count preserved (no re-apply).
    outage_summary = await reconcile_unresolved_effects(
        sessions_fixture, StatusOutageAdapter(sessions_fixture), limit=1,
    )
    assert outage_summary["scanned"] == 1
    assert outage_summary["still_unresolved"] == 1
    assert outage_summary["reconciled_verified"] == 0
    assert outage_summary["provider_success_claimed"] is False

    # Bounded poller reconciles 1 unresolved receipt per run without touching attempt_count.
    adapter = SimulatorAdapter(sessions_fixture)
    first_pass = await reconcile_unresolved_effects(sessions_fixture, adapter, limit=1)
    assert first_pass["scanned"] == 1
    assert first_pass["reconciled_verified"] == 1
    assert first_pass["provider_success_claimed"] is False
    second_pass = await reconcile_unresolved_effects(sessions_fixture, adapter, limit=10)
    assert second_pass["scanned"] == 1
    assert second_pass["reconciled_verified"] == 1

    async with sessions_fixture() as session:
        r_a = await session.get(EffectReceipt, eid_a)
        r_b = await session.get(EffectReceipt, eid_b)
        assert r_a.state == "VERIFIED" and r_a.attempt_count == 1
        assert r_b.state == "VERIFIED" and r_b.attempt_count == 1


@pytest.mark.asyncio
async def test_batch_outbox_claim_and_partial_dispatch_preserves_causal_order(sessions_fixture):
    async with sessions_fixture() as session:
        inc_1 = await admit_incident(session, "t", "batch-1", IncidentCreate(target_id="svc-1"))
    async with sessions_fixture() as session:
        inc_2 = await admit_incident(session, "t", "batch-2", IncidentCreate(target_id="svc-2"))
    # Enqueue a follow-up signal on inc_1; START_WORKFLOW must still block it until delivered.
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutboxEntry(incident_id=inc_1.id, kind="CANCEL_WORKFLOW"))

    claims = await claim_intent_batch(sessions_fixture, limit=10)
    assert len(claims) == 2
    assert {c[2] for c in claims} == {str(inc_1.id), str(inc_2.id)}
    assert all(c[3] == "START_WORKFLOW" for c in claims)
    for claim in claims:
        assert await acknowledge_claim(sessions_fixture, claim)

    # Enqueue another incident and test partial dispatch failure isolation.
    async with sessions_fixture() as session:
        inc_3 = await admit_incident(session, "t", "batch-3", IncidentCreate(target_id="svc-3"))

    class PartialClient:
        async def start_workflow(self, *args, **kwargs):
            if str(inc_3.id) in kwargs["id"]:
                raise RuntimeError("temporal start failed")
            return None

        def get_workflow_handle(self, wid):
            class _Handle:
                async def signal(self, *args, **kwargs):
                    return None
            return _Handle()

    count = await dispatch_batch(PartialClient(), sessions_fixture, limit=10)
    assert count == 2
    async with sessions_fixture() as session:
        cancel_entry = await session.scalar(
            select(OutboxEntry).where(
                OutboxEntry.incident_id == inc_1.id,
                OutboxEntry.kind == "CANCEL_WORKFLOW",
            )
        )
        start_3 = await session.scalar(
            select(OutboxEntry).where(
                OutboxEntry.incident_id == inc_3.id,
                OutboxEntry.kind == "START_WORKFLOW",
            )
        )
        assert cancel_entry.delivered_at is not None
        assert start_3.delivered_at is None
        assert start_3.attempt == 1
