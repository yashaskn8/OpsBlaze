import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from uuid import uuid4
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.investigator.baseline import inspect
from backend.models import (
    Evidence, HypothesisStep, Incident, ProbeCall, ProbeCapability, SimulatorTarget
)
from backend.probes import gateway
from backend.probes.gateway import (
    _ACTIVE_TARGET_PROBES, ProbeDenied, invoke_probe, redact_evidence_excerpt,
    reserve_external_probe_intent,
)
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.mark.asyncio
async def test_allowlisted_probe_is_tenant_scoped_and_deduplicated(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t1", "k1", IncidentCreate(target_id="checkout"))
    first = await invoke_probe(sessions_fixture, "t1", incident.id, 1, "simulator.health")
    second = await invoke_probe(sessions_fixture, "t1", incident.id, 1, "simulator.health")
    assert first == second
    with pytest.raises(ProbeDenied):
        await invoke_probe(sessions_fixture, "other", incident.id, 2, "simulator.health")
    with pytest.raises(ProbeDenied):
        await invoke_probe(sessions_fixture, "t1", incident.id, 1, "simulator.version")
    async with sessions_fixture() as session:
        row = await session.get(Incident, incident.id)
        assert row.probe_used == 1
        assert len(list((await session.scalars(select(Evidence))).all())) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_completed_probe_drops_idle_target_counter(sessions_fixture, fails):
    tenant = f"probe-counter-{uuid4().hex}"
    async with sessions_fixture() as session:
        incident = await admit_incident(session, tenant, "counter", IncidentCreate(target_id="checkout"))

    if fails:
        with pytest.raises(ProbeDenied, match="superseded evidence"):
            await invoke_probe(
                sessions_fixture, tenant, incident.id, 1, "simulator.health",
                supersedes_evidence_id=uuid4(),
            )
    else:
        await invoke_probe(sessions_fixture, tenant, incident.id, 1, "simulator.health")

    assert (tenant, "checkout") not in _ACTIVE_TARGET_PROBES


@pytest.mark.asyncio
async def test_rate_denial_replays_without_taking_another_active_probe_slot(sessions_fixture, monkeypatch):
    tenant = f"probe-rate-{uuid4().hex}"
    monkeypatch.setitem(gateway.PROBE_CATALOG["simulator.health"], "rate_limit_per_minute", 1)
    async with sessions_fixture() as session:
        first = await admit_incident(session, tenant, "first", IncidentCreate(target_id="checkout"))
        second = await admit_incident(session, tenant, "second", IncidentCreate(target_id="checkout"))

    await invoke_probe(sessions_fixture, tenant, first.id, 1, "simulator.health")
    key = (tenant, "checkout")
    monkeypatch.setitem(_ACTIVE_TARGET_PROBES, key, 1)
    denied = await invoke_probe(sessions_fixture, tenant, second.id, 1, "simulator.health")
    assert denied.outcome == "DENIED" and denied.evidence_id is None
    assert await invoke_probe(sessions_fixture, tenant, second.id, 1, "simulator.health") == denied
    assert _ACTIVE_TARGET_PROBES[key] == 1
    async with sessions_fixture() as session:
        stored = await session.get(Incident, second.id)
        assert stored.probe_used == 1


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("reserve_only", [False, True])
async def test_native_probe_capability_expiry_includes_incident_lock_wait(
    native_sessions_fixture, monkeypatch, reserve_only,
):
    sessions = native_sessions_fixture
    tenant = f"probe-expiry-{uuid4().hex}"
    async with sessions() as session:
        incident = await admit_incident(session, tenant, "expiry", IncidentCreate(target_id="checkout"))
    now = datetime.now(timezone.utc)
    first = await invoke_probe(sessions, tenant, incident.id, 1, "simulator.health", now=now)
    assert first.evidence_id is not None
    lock_requested = asyncio.Event()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    class WaitingSession(AsyncSession):
        async def scalar(self, statement, *args, **kwargs):
            if statement.column_descriptions[0]["entity"] is Incident:
                lock_requested.set()
            return await super().scalar(statement, *args, **kwargs)

    monkeypatch.setattr(gateway, "datetime", Clock)
    contenders = async_sessionmaker(sessions.kw["bind"], class_=WaitingSession)

    async def checked_probe():
        if reserve_only:
            with pytest.raises(ProbeDenied, match="expired"):
                await reserve_external_probe_intent(contenders, tenant, incident.id, 2, "simulator.health")
        else:
            result = await invoke_probe(contenders, tenant, incident.id, 2, "simulator.health")
            assert result.outcome == "DENIED" and result.evidence_id is None

    async with asyncio.TaskGroup() as tasks:
        async with sessions.begin() as session:
            await session.get(Incident, incident.id, with_for_update=True)
            pending = tasks.create_task(checked_probe())
            await asyncio.wait_for(lock_requested.wait(), timeout=5)
            assert not pending.done()
            now += timedelta(seconds=301)

    async with sessions() as session:
        capability = await session.scalar(select(ProbeCapability).where(ProbeCapability.incident_id == incident.id))
        assert capability.used_calls == 1
        assert (await session.scalars(select(Evidence.id).where(Evidence.incident_id == incident.id))).all() == [first.evidence_id]


@pytest.mark.asyncio
async def test_forbidden_probe_denied_without_external_effect(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t1", "k1", IncidentCreate(target_id="checkout"))
    result = await invoke_probe(sessions_fixture, "t1", incident.id, 1, "shell.exec")
    assert result.outcome == "DENIED"
    assert result.evidence_id is None
    async with sessions_fixture() as session:
        row = await session.get(Incident, incident.id)
        assert row.probe_used == 1
        assert len(list((await session.scalars(select(Evidence))).all())) == 0


@pytest.mark.asyncio
async def test_investigator_adapts_to_first_probe_and_records_steps(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(tenant_id="t1", target_id="checkout", version=3, healthy=False, last_fence=0))
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t1", "k1", IncidentCreate(target_id="checkout"))
    assert await inspect(sessions_fixture, "t1", incident.id) == "SIMULATOR_REVIEW_REQUIRED"
    assert await inspect(sessions_fixture, "t1", incident.id) == "SIMULATOR_REVIEW_REQUIRED"
    async with sessions_fixture() as session:
        steps = list((await session.scalars(select(HypothesisStep).order_by(HypothesisStep.step_no))).all())
        assert [step.step_no for step in steps] == [1, 2]
        assert all(step.evidence_ids for step in steps)
        incident = await session.get(Incident, incident.id)
        assert incident.probe_used == 2


@pytest.mark.asyncio
async def test_missing_simulator_target_abstains(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t1", "k1", IncidentCreate(target_id="missing"))
    assert await inspect(sessions_fixture, "t1", incident.id) == "ABSTAINED"


@pytest.mark.asyncio
async def test_new_probe_steps_are_monotonic_without_spending_on_gaps(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t1", "step-gap", IncidentCreate(target_id="checkout")
        )
    with pytest.raises(ProbeDenied, match="sequential"):
        await invoke_probe(
            sessions_fixture, "t1", incident.id, 2, "simulator.health"
        )
    async with sessions_fixture() as session:
        incident_row = await session.get(Incident, incident.id)
        assert incident_row.probe_used == 0
        assert (await session.scalars(select(ProbeCall))).all() == []
    first = await invoke_probe(
        sessions_fixture, "t1", incident.id, 1, "simulator.health"
    )
    assert first.outcome == "UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["hash", "source", "tenant", "schema"])
async def test_replayed_probe_rejects_tampered_evidence(sessions_fixture, damage):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(
                SimulatorTarget(
                    tenant_id="t1", target_id="checkout",
                    version=3, healthy=False, last_fence=0,
                )
            )
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t1", f"tamper-{damage}", IncidentCreate(target_id="checkout")
        )
    first = await invoke_probe(
        sessions_fixture, "t1", incident.id, 1, "simulator.health"
    )
    assert first.outcome == "OK"

    async with sessions_fixture() as session:
        async with session.begin():
            evidence = await session.get(Evidence, first.evidence_id)
            if damage == "hash":
                evidence.redacted_excerpt = '{"healthy":true}'
            elif damage == "source":
                evidence.source = "simulator.version"
            elif damage == "tenant":
                evidence.tenant_id = "other"
            else:
                evidence.redacted_excerpt = '{"healthy":"false"}'
                evidence.sha256 = hashlib.sha256(
                    evidence.redacted_excerpt.encode()
                ).hexdigest()

    with pytest.raises(ProbeDenied, match="recorded probe"):
        await invoke_probe(
            sessions_fixture, "t1", incident.id, 1, "simulator.health"
        )
    async with sessions_fixture() as session:
        incident_row = await session.get(Incident, incident.id)
        assert incident_row.probe_used == 1


@pytest.mark.asyncio
async def test_replay_rejects_cross_incident_evidence_link(sessions_fixture):
    async with sessions_fixture() as session:
        first = await admit_incident(
            session, "t", "probe-a", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        second = await admit_incident(
            session, "t", "probe-b", IncidentCreate(target_id="checkout")
        )
    await invoke_probe(sessions_fixture, "t", first.id, 1, "simulator.health")
    other_result = await invoke_probe(
        sessions_fixture, "t", second.id, 1, "simulator.health"
    )
    async with sessions_fixture() as session:
        async with session.begin():
            row = await session.scalar(
                select(ProbeCall).where(ProbeCall.incident_id == first.id)
            )
            row.evidence_id = other_result.evidence_id
    with pytest.raises(ProbeDenied, match="integrity"):
        await invoke_probe(sessions_fixture, "t", first.id, 1, "simulator.health")


@pytest.mark.asyncio
async def test_external_probe_intent_commits_reservation_before_read_and_survives_restart(
    sessions_fixture,
):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t-ext", "ext-reserve-1", IncidentCreate(target_id="checkout")
        )
    call_id = await reserve_external_probe_intent(
        sessions_fixture, "t-ext", incident.id, 1, "simulator.health"
    )
    # Re-open a clean session to simulate a process restart.
    async with sessions_fixture() as session:
        row = await session.get(ProbeCall, call_id)
        assert row is not None
        assert row.outcome == "RESERVED"
        assert row.evidence_id is None
        inc_row = await session.get(Incident, incident.id)
        assert inc_row.probe_used == 1
        cap = await session.scalar(
            select(ProbeCapability).where(
                ProbeCapability.incident_id == incident.id,
                ProbeCapability.catalog_id == "simulator.health",
            )
        )
        assert cap is not None and cap.used_calls == 1
    with pytest.raises(ProbeDenied, match="already recorded"):
        await reserve_external_probe_intent(
            sessions_fixture, "t-ext", incident.id, 1, "simulator.health"
        )


def test_redact_evidence_excerpt_strips_secrets_and_bounds_size():
    clean, hits = redact_evidence_excerpt('{"healthy":false}')
    assert clean == '{"healthy":false}' and hits == 0

    noisy = (
        "auth=Bearer secret_tok_abc123 key=sk-abcdefghijklm "
        "password=hunter2 -----BEGIN PRIVATE " + "KEY-----abc-----END PRIVATE " + "KEY-----"
    )
    redacted, red_hits = redact_evidence_excerpt(noisy)
    assert red_hits >= 4
    assert "secret_tok_abc123" not in redacted
    assert "sk-abcdefghijklm" not in redacted
    assert "hunter2" not in redacted
    assert "BEGIN PRIVATE KEY" not in redacted

    with pytest.raises(ProbeDenied, match="maximum size"):
        redact_evidence_excerpt("x" * 600, max_bytes=512)
