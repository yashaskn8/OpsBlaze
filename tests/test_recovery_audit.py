"""G3 cross-system red team: a read-only audit cannot retry or invent success."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models import EffectReceipt, Incident, OutboxEntry
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.workers.dispatcher import workflow_id
from backend.workers.recovery_audit import (
    audit_exit_code, audit_recovery_consistency, check_receipt,
)


class FakeWorkflow:
    def __init__(self, value):
        self.value = value

    async def describe(self):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class FakeTemporal:
    def __init__(self, descriptions=None):
        self.descriptions = descriptions or {}
        self.lookups = []

    def get_workflow_handle(self, wid):
        self.lookups.append(wid)
        return FakeWorkflow(
            self.descriptions.get(wid, ConnectionError("history offline"))
        )


def description(wid, status="RUNNING", *, actual_id=None, kind="IncidentWorkflow"):
    return SimpleNamespace(
        id=wid if actual_id is None else actual_id,
        status=SimpleNamespace(name=status),
        workflow_type=kind,
    )


async def incident_start(sessions, client_key="case"):
    async with sessions() as session:
        incident = await admit_incident(
            session, "audit-tenant", client_key, IncidentCreate(
                target_id="synthetic-audit-target"
            ),
        )
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    async with sessions() as session:
        async with session.begin():
            row = await session.get(Incident, incident.id)
            row.status_projection = "EXECUTING"
            row.phase_entered_at = old
            start = await session.scalar(select(OutboxEntry).where(
                OutboxEntry.incident_id == incident.id,
                OutboxEntry.kind == "START_WORKFLOW",
            ))
            start.created_at = old
            start.attempt = 2
            start.available_at = old
            return incident.id, start.id


@pytest.mark.asyncio
async def test_lost_start_ack_reports_remote_commit_without_acknowledging_it(
    sessions_fixture,
):
    iid, entry_id = await incident_start(sessions_fixture)
    wid = workflow_id("audit-tenant", iid)
    client = FakeTemporal({wid: description(wid)})
    report = await audit_recovery_consistency(sessions_fixture, client)
    assert report["status"] == "AUDIT_ONLY"
    assert report["scanned"] == 1 and not report["scan_truncated"]
    assert not report["recovery_certified"]
    assert not report["external_effects_enabled"]
    assert client.lookups == [wid]
    finding = report["findings"][0]
    assert finding["outbox_id"] == entry_id
    assert finding["temporal_status"] == "RUNNING"
    assert finding["temporal_identity_verified"] is True
    assert finding["start_acknowledged"] is False
    assert finding["reasons"] == ["TEMPORAL_START_UNACKNOWLEDGED_AFTER_REMOTE_COMMIT"]
    assert finding["safe_action"] == "INSPECT_ONLY"
    assert "audit-tenant" not in str(report)
    assert "synthetic-audit-target" not in str(report)
    async with sessions_fixture() as session:
        start = await session.get(OutboxEntry, entry_id)
        assert start.attempt == 2 and start.delivered_at is None


@pytest.mark.asyncio
async def test_temporal_outage_cannot_be_mistaken_for_no_remote_start(sessions_fixture):
    iid, _ = await incident_start(sessions_fixture)
    wid = workflow_id("audit-tenant", iid)
    report = await audit_recovery_consistency(
        sessions_fixture, FakeTemporal({wid: TimeoutError("no endpoint")}),
    )
    finding = report["findings"][0]
    assert finding["temporal_status"] == "UNKNOWN"
    assert not finding["temporal_identity_verified"]
    assert finding["reasons"] == [
        "TEMPORAL_HISTORY_UNAVAILABLE_OR_UNKNOWN",
        "UNACKNOWLEDGED_START_CANNOT_BE_SAFELY_REPLAYED_FROM_AUDIT",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["wrong_id", "wrong_type", "missing_type", "bad_status"])
async def test_untrusted_temporal_description_never_proves_remote_start(
    sessions_fixture, fault,
):
    iid, _ = await incident_start(sessions_fixture)
    wid = workflow_id("audit-tenant", iid)
    kwargs = {
        "wrong_id": {"actual_id": "opsblaze/other/forged"},
        "wrong_type": {"kind": "ImpersonatingWorkflow"},
        "missing_type": {"kind": None},
        "bad_status": {"status": "DEFINITELY_VERIFIED"},
    }[fault]
    report = await audit_recovery_consistency(
        sessions_fixture, FakeTemporal({wid: description(wid, **kwargs)}),
    )
    finding = report["findings"][0]
    assert not finding["temporal_identity_verified"]
    assert "TEMPORAL_START_UNACKNOWLEDGED_AFTER_REMOTE_COMMIT" not in finding["reasons"]
    assert any(
        reason in finding["reasons"]
        for reason in (
            "TEMPORAL_HISTORY_IDENTITY_UNVERIFIED",
            "TEMPORAL_HISTORY_UNAVAILABLE_OR_UNKNOWN",
        )
    )


@pytest.mark.asyncio
async def test_delivered_start_with_terminal_history_cannot_self_repair_projection(
    sessions_fixture,
):
    iid, entry = await incident_start(sessions_fixture)
    async with sessions_fixture() as session:
        async with session.begin():
            start = await session.get(OutboxEntry, entry)
            start.delivered_at = datetime.now(timezone.utc) - timedelta(hours=1)
    wid = workflow_id("audit-tenant", iid)
    result = await audit_recovery_consistency(
        sessions_fixture, FakeTemporal({wid: description(wid, "COMPLETED")}),
    )
    finding = result["findings"][0]
    assert finding["reasons"] == ["PROJECTION_LAGS_TERMINAL_HISTORY"]
    async with sessions_fixture() as session:
        incident = await session.get(Incident, iid)
        assert incident.status_projection == "EXECUTING"


@pytest.mark.asyncio
async def test_running_history_with_terminal_db_projection_is_reported(
    sessions_fixture,
):
    iid, entry = await incident_start(sessions_fixture)
    async with sessions_fixture() as session:
        async with session.begin():
            incident = await session.get(Incident, iid)
            incident.status_projection = "VERIFIED"
            start = await session.get(OutboxEntry, entry)
            start.delivered_at = datetime.now(timezone.utc)
    wid = workflow_id("audit-tenant", iid)
    result = await audit_recovery_consistency(
        sessions_fixture, FakeTemporal({wid: description(wid)}),
    )
    assert result["findings"][0]["reasons"] == [
        "TERMINAL_PROJECTION_WHILE_TEMPORAL_RUNNING"
    ]


@pytest.mark.asyncio
async def test_effect_receipt_uncertainty_and_invalid_success_are_reported(
    sessions_fixture,
):
    iid, _ = await incident_start(sessions_fixture)
    async with sessions_fixture() as session:
        async with session.begin():
            session.add_all([
                EffectReceipt(
                    id=uuid4(), tenant_id="audit-tenant", incident_id=iid,
                    plan_digest="c" * 64, target_id="synthetic-audit-target",
                    adapter_id="simulator", permit_digest="a" * 64,
                    idempotency_key="pending-audit-effect",
                    fence=1, expected_version=1, attempt_count=1,
                    observed_version=None, state="UNKNOWN",
                ),
                EffectReceipt(
                    id=uuid4(), tenant_id="audit-tenant", incident_id=iid,
                    plan_digest="d" * 64, target_id="synthetic-audit-target",
                    adapter_id="simulator", permit_digest=None,
                    idempotency_key="forged-audit-effect",
                    fence=2, expected_version=3, attempt_count=2,
                    observed_version=4, state="VERIFIED",
                ),
            ])
    wid = workflow_id("audit-tenant", iid)
    report = await audit_recovery_consistency(
        sessions_fixture, FakeTemporal({wid: description(wid)}),
    )
    reasons = report["findings"][0]["reasons"]
    assert "UNRESOLVED_EFFECT_REQUIRES_RECONCILIATION" in reasons
    assert "RECEIPT_INVARIANT_VIOLATION" in reasons
    assert "reconcile" not in report["status"].lower()


@pytest.mark.asyncio
async def test_duplicate_start_intents_and_conflicting_terminal_flags(sessions_fixture):
    iid, entry = await incident_start(sessions_fixture)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    async with sessions_fixture() as session:
        async with session.begin():
            first = await session.get(OutboxEntry, entry)
            first.delivered_at = old
            first.dead_lettered_at = old
            session.add(OutboxEntry(
                incident_id=iid, kind="START_WORKFLOW",
                created_at=old, available_at=old, attempt=1,
            ))
    wid = workflow_id("audit-tenant", iid)
    report = await audit_recovery_consistency(
        sessions_fixture, FakeTemporal({wid: description(wid)}),
    )
    assert report["scanned"] == 2
    assert all("DUPLICATE_START_INTENT" in f["reasons"] for f in report["findings"])
    assert "CONFLICTING_OUTBOX_TERMINAL_MARKERS" in report["findings"][0]["reasons"]


@pytest.mark.asyncio
async def test_scan_is_bounded_and_reports_incomplete_view(sessions_fixture):
    ids = [await incident_start(sessions_fixture, f"case-{i}") for i in range(3)]
    client = FakeTemporal()
    result = await audit_recovery_consistency(
        sessions_fixture, client, limit=1,
    )
    assert result["scanned"] == 1 and result["scan_truncated"]
    assert len(client.lookups) == 1
    assert all(f["safe_action"] == "INSPECT_ONLY" for f in result["findings"])
    assert len(ids) == 3



@pytest.mark.asyncio
async def test_recovery_audit_cursor_pages_all_old_starts_once(sessions_fixture):
    await incident_start(sessions_fixture, "first")
    await incident_start(sessions_fixture, "second")
    await incident_start(sessions_fixture, "third")
    client = FakeTemporal()
    cursor = 0
    scanned = []
    for expected_count in (1, 1, 1):
        page = await audit_recovery_consistency(
            sessions_fixture, client, limit=1, after_id=cursor,
        )
        assert page["after_id"] == cursor
        assert page["scanned"] == expected_count
        scanned.append(page["findings"][0]["outbox_id"])
        if page["scan_truncated"]:
            assert page["next_after_id"] == scanned[-1]
            assert page["next_after_id"] > cursor
            assert audit_exit_code(page) == 2
            cursor = page["next_after_id"]
        else:
            assert page["next_after_id"] is None
    assert len(set(scanned)) == 3
    assert scanned == sorted(scanned)
    assert len(client.lookups) == 3


@pytest.mark.asyncio
async def test_high_water_mark_excludes_new_starts_between_pages(sessions_fixture):
    await incident_start(sessions_fixture, "initial-a")
    await incident_start(sessions_fixture, "initial-b")
    observed = datetime.now(timezone.utc)
    first = await audit_recovery_consistency(
        sessions_fixture, FakeTemporal(), limit=1, now=observed,
    )
    assert first["scan_truncated"]
    assert first["through_id"] >= first["next_after_id"]
    new_id, _ = await incident_start(sessions_fixture, "inserted-later")
    second = await audit_recovery_consistency(
        sessions_fixture, FakeTemporal(), limit=1, now=observed,
        after_id=first["next_after_id"], through_id=first["through_id"],
    )
    assert second["scanned"] == 1
    assert second["through_id"] == first["through_id"]
    assert second["next_after_id"] is None
    assert str(new_id) not in [f["incident_id"] for f in second["findings"]]
    # A fresh independent scan is permitted to see the new durable intent.
    fresh = await audit_recovery_consistency(
        sessions_fixture, FakeTemporal(), now=observed,
    )
    assert fresh["scanned"] == 3


@pytest.mark.asyncio
async def test_fixed_observation_time_keeps_age_cutoff_stable_across_pages(
    sessions_fixture,
):
    old_id, _ = await incident_start(sessions_fixture, "old")
    future_id, future_start = await incident_start(sessions_fixture, "future")
    fixed = datetime.now(timezone.utc)
    async with sessions_fixture() as session:
        async with session.begin():
            row = await session.get(OutboxEntry, future_start)
            row.created_at = fixed - timedelta(seconds=10)
    first = await audit_recovery_consistency(
        sessions_fixture, FakeTemporal(), now=fixed, stale_seconds=300, limit=1,
    )
    assert first["observed_at"] == fixed.isoformat()
    assert first["scanned"] == 1
    assert first["findings"][0]["incident_id"] == str(old_id)
    assert first["next_after_id"] is None

    # A later audit can include the second record, but only if the
    # observation time is intentionally advanced.
    later = await audit_recovery_consistency(
        sessions_fixture, FakeTemporal(),
        now=fixed + timedelta(seconds=400), stale_seconds=300,
    )
    assert {row["incident_id"] for row in later["findings"]} == {
        str(old_id), str(future_id),
    }


@pytest.mark.asyncio
async def test_duplicate_start_across_pages_is_detected_on_each_page(sessions_fixture):
    iid, first_id = await incident_start(sessions_fixture)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutboxEntry(
                incident_id=iid, kind="START_WORKFLOW",
                created_at=old, available_at=old, attempt=1,
            ))
    wid = workflow_id("audit-tenant", iid)
    client = FakeTemporal({wid: description(wid)})
    first = await audit_recovery_consistency(
        sessions_fixture, client, limit=1,
    )
    assert first["findings"][0]["outbox_id"] == first_id
    assert "DUPLICATE_START_INTENT" in first["findings"][0]["reasons"]
    assert first["scan_truncated"]
    second = await audit_recovery_consistency(
        sessions_fixture, client, limit=1, after_id=first["next_after_id"],
    )
    assert "DUPLICATE_START_INTENT" in second["findings"][0]["reasons"]
    assert not second["scan_truncated"]


@pytest.mark.asyncio
async def test_unattempted_starts_do_not_generate_false_temporal_loss(sessions_fixture):
    async with sessions_fixture() as session:
        await admit_incident(
            session, "audit-tenant", "new",
            IncidentCreate(target_id="synthetic-target"),
        )
    report = await audit_recovery_consistency(
        sessions_fixture, FakeTemporal(), stale_seconds=1,
        now=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    assert report["scanned"] == 0


@pytest.mark.parametrize("args", [
    {"stale_seconds": True}, {"stale_seconds": 0},
    {"stale_seconds": 86401}, {"limit": False},
    {"limit": 0}, {"limit": 51},
    {"now": datetime(2026, 11, 22)},
    {"after_id": True}, {"after_id": -1}, {"after_id": 1.5},
    {"through_id": True}, {"through_id": -1}, {"through_id": 1.5},
    {"now": "2026-11-22T00:00:00Z"},
])
@pytest.mark.asyncio
async def test_recovery_audit_rejects_invalid_bounds(sessions_fixture, args):
    with pytest.raises(ValueError):
        await audit_recovery_consistency(
            sessions_fixture, FakeTemporal(), **args,
        )


@pytest.mark.parametrize(("row", "expected"), [
    (("UNKNOWN", "simulator", "a" * 64, 1, 0, None),
     "UNRESOLVED_EFFECT_REQUIRES_RECONCILIATION"),
    (("VERIFIED", "simulator", None, 1, 0, 1),
     "RECEIPT_INVARIANT_VIOLATION"),
    (("VERIFIED", "simulator", "a" * 64, 2, 0, 1),
     "RECEIPT_INVARIANT_VIOLATION"),
    (("CONFIRMED", "simulator", "a" * 64, 1, 0, 3),
     "RECEIPT_INVARIANT_VIOLATION"),
    (("VERIFIED", "simulator", "a" * 64, 1, 0, 1), None),
    (("VERIFIED", "live-provider", "a" * 64, 1, 0, 1),
     "RECEIPT_INVARIANT_VIOLATION"),
])
def test_no_unverified_receipt_can_be_inferred_successful(row, expected):
    assert check_receipt(*row) == expected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_native_temporal_description_binds_stable_workflow_identity(
    sessions_fixture,
):
    from temporalio.testing import WorkflowEnvironment

    from backend.workflows.incident import IncidentWorkflow

    iid, _ = await incident_start(sessions_fixture)
    wid = workflow_id("audit-tenant", iid)
    async with await WorkflowEnvironment.start_local() as env:
        await env.client.start_workflow(
            IncidentWorkflow.run,
            args=["audit-tenant", str(iid)],
            id=wid, task_queue=f"recovery-audit-{uuid4()}",
        )
        result = await audit_recovery_consistency(
            sessions_fixture, env.client,
        )
    finding = result["findings"][0]
    assert finding["temporal_status"] == "RUNNING"
    assert finding["temporal_identity_verified"] is True
    assert finding["reasons"] == [
        "TEMPORAL_START_UNACKNOWLEDGED_AFTER_REMOTE_COMMIT"
    ]


@pytest.mark.asyncio
async def test_receipt_scan_overflow_fails_closed_without_unbounded_fetch(
    sessions_fixture,
):
    iid, _ = await incident_start(sessions_fixture)
    async with sessions_fixture() as session:
        async with session.begin():
            for count in range(9):
                session.add(EffectReceipt(
                    id=uuid4(), tenant_id="audit-tenant", incident_id=iid,
                    plan_digest="a" * 64, target_id="synthetic-audit-target",
                    adapter_id="simulator", permit_digest="b" * 64,
                    idempotency_key=f"overflow-{count}", fence=count + 1,
                    expected_version=0, state="UNKNOWN",
                    attempt_count=1, observed_version=None,
                ))
    wid = workflow_id("audit-tenant", iid)
    result = await audit_recovery_consistency(
        sessions_fixture, FakeTemporal({wid: description(wid)}), limit=1,
    )
    assert result["receipt_scan_truncated"] is True
    assert "RECEIPT_SCAN_TRUNCATED" in result["findings"][0]["reasons"]
    assert not result["recovery_certified"]


@pytest.mark.parametrize(("reasons", "start_overflow", "receipt_overflow", "expected"), [
    ([], False, False, 0),
    (["TEMPORAL_HISTORY_UNAVAILABLE_OR_UNKNOWN"], False, False, 1),
    ([], True, False, 2),
    (["RECEIPT_INVARIANT_VIOLATION"], False, True, 2),
])
def test_recovery_audit_process_exit_code(
    reasons, start_overflow, receipt_overflow, expected,
):
    report = {
        "findings": [{"reasons": reasons}],
        "scan_truncated": start_overflow,
        "receipt_scan_truncated": receipt_overflow,
    }
    assert audit_exit_code(report) == expected
