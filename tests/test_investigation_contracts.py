"""A1-A3 investigation contracts: capabilities, manifests, supersession, competing hypotheses & metrics."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.api import app
from backend.database import get_session
from backend.investigator.baseline import RUNBOOK_REVISION, inspect
from backend.models import (
    ContextManifest,
    Evidence,
    HypothesisStep,
    Incident,
    OutboxEntry,
    ProbeCapability,
    SimulatorTarget,
)
from backend.probes.gateway import (
    CATALOG_VERSION,
    ProbeDenied,
    _ACTIVE_TARGET_PROBES,
    classify_evidence_freshness,
    invoke_probe,
    issue_incident_capabilities,
    refresh_evidence,
    revoke_incident_capability,
)
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings, get_settings


@pytest.mark.asyncio
async def test_a2_capability_issuance_revocation_expiry_and_budgets(sessions_fixture):
    """A2-02 & A2-06: versioned expiring revocable capabilities, size & target concurrency bounds."""
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(
                tenant_id="t1", target_id="checkout", version=2, healthy=False, last_fence=0,
            ))
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t1", "cap-1", IncidentCreate(target_id="checkout"))

    with pytest.raises(ProbeDenied, match="allowlisted"):
        await issue_incident_capabilities(
            sessions_fixture, "t1", incident.id, catalog_ids=["shell.exec"],
        )

    caps = await issue_incident_capabilities(
        sessions_fixture, "t1", incident.id, ttl_seconds=60, max_calls=1,
    )
    assert len(caps) == 2
    assert all(c.catalog_version == CATALOG_VERSION for c in caps)

    # Revoking simulator.version causes subsequent step to be DENIED cleanly.
    async with sessions_fixture() as session:
        revoked = await revoke_incident_capability(
            session, "t1", incident.id, "simulator.version", "sec-op",
        )
        assert revoked is not None and revoked.version == 2 and revoked.revoked_at is not None

    first = await invoke_probe(sessions_fixture, "t1", incident.id, 1, "simulator.health")
    assert first.outcome == "OK"
    second = await invoke_probe(sessions_fixture, "t1", incident.id, 2, "simulator.version")
    assert second.outcome == "DENIED"

    # Expired capability is denied.
    async with sessions_fixture() as session:
        inc2 = await admit_incident(session, "t1", "cap-exp", IncidentCreate(target_id="checkout"))
    await issue_incident_capabilities(sessions_fixture, "t1", inc2.id, ttl_seconds=10)
    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    denied_exp = await invoke_probe(sessions_fixture, "t1", inc2.id, 1, "simulator.health", now=future)
    assert denied_exp.outcome == "DENIED"

    # Per-target concurrency limit fails closed without consuming probe budget.
    async with sessions_fixture() as session:
        inc3 = await admit_incident(session, "t1", "cap-conc", IncidentCreate(target_id="checkout"))
    _ACTIVE_TARGET_PROBES[("t1", "checkout")] = 2
    try:
        with pytest.raises(ProbeDenied, match="concurrency"):
            await invoke_probe(sessions_fixture, "t1", inc3.id, 1, "simulator.health")
    finally:
        _ACTIVE_TARGET_PROBES[("t1", "checkout")] = 0


@pytest.mark.asyncio
async def test_a3_stale_labeling_append_only_supersession_and_immutable_manifests(sessions_fixture):
    """A3-03, A3-04, A3-06: freshness labels, append-only supersession, immutable context manifests."""
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(
                tenant_id="t1", target_id="checkout", version=1, healthy=False, last_fence=0,
            ))
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t1", "sup-1", IncidentCreate(target_id="checkout"))

    assert await inspect(sessions_fixture, "t1", incident.id) == "SIMULATOR_REVIEW_REQUIRED"

    async with sessions_fixture() as session:
        manifests = (await session.scalars(
            select(ContextManifest)
            .where(ContextManifest.tenant_id == "t1", ContextManifest.incident_id == incident.id)
            .order_by(ContextManifest.step_no)
        )).all()
        assert [m.step_no for m in manifests] == [1, 2]
        assert all(m.runbook_revision == RUNBOOK_REVISION for m in manifests)
        assert all(len(m.manifest_digest) == 64 for m in manifests)
        assert all(m.memory_mode == "NO_MEMORY" for m in manifests)

        steps = (await session.scalars(
            select(HypothesisStep)
            .where(HypothesisStep.incident_id == incident.id)
            .order_by(HypothesisStep.step_no)
        )).all()
        assert len(steps) == 2
        assert steps[0].competing_hypotheses is not None
        assert any(h["status"] == "REFUTED" and h["counter_evidence_ids"] for h in steps[0].competing_hypotheses)
        assert any(h["status"] == "SUPPORTED" and len(h["supporting_evidence_ids"]) == 2 for h in steps[1].competing_hypotheses)
        assert all(isinstance(s.elapsed_ms, int) and s.elapsed_ms >= 0 for s in steps)

        first_ev = await session.scalar(
            select(Evidence).where(
                Evidence.incident_id == incident.id, Evidence.source == "simulator.health",
            )
        )
        assert classify_evidence_freshness(first_ev) == "FRESH"
        assert classify_evidence_freshness(
            first_ev, now=datetime.now(timezone.utc) + timedelta(minutes=10),
        ) == "STALE"
        orig_id, orig_hash = first_ev.id, first_ev.sha256

    # Update target version and refresh evidence append-only (preserving historical record).
    async with sessions_fixture() as session:
        async with session.begin():
            target = await session.get(SimulatorTarget, ("t1", "checkout"))
            target.version = 2
            target.healthy = True

    refreshed = await refresh_evidence(sessions_fixture, "t1", incident.id, orig_id)
    assert refreshed.outcome == "OK"
    assert refreshed.evidence_id != orig_id
    async with sessions_fixture() as session:
        orig_after = await session.get(Evidence, orig_id)
        new_ev = await session.get(Evidence, refreshed.evidence_id)
        assert orig_after.sha256 == orig_hash
        assert orig_after.target_version == 1
        assert new_ev.supersedes_evidence_id == orig_id
        assert new_ev.target_version == 2
        # Original step-1 manifest still references only the original historical evidence ID.
        m1 = await session.scalar(
            select(ContextManifest).where(
                ContextManifest.incident_id == incident.id, ContextManifest.step_no == 1,
            )
        )
        assert m1.evidence_ids == [str(orig_id)]


@pytest.mark.asyncio
async def test_a1_timeout_ceiling_and_api_endpoints_for_catalog_pages_metrics_and_manual_trigger(
    sessions_fixture,
):
    """A1-06, A1-08, A1-09, A2-07, A3-05: elapsed bound, paged reads, catalog, metrics, manual trigger."""
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(
                tenant_id="alpha", target_id="checkout", version=1, healthy=False, last_fence=0,
            ))
    async with sessions_fixture() as session:
        inc = await admit_incident(session, "alpha", "api-1", IncidentCreate(target_id="checkout"))
    async with sessions_fixture() as session:
        foreign = await admit_incident(session, "beta", "api-foreign", IncidentCreate(target_id="secret"))

    # Zero elapsed budget triggers clean timeout abstention.
    assert await inspect(sessions_fixture, "alpha", inc.id, max_elapsed_seconds=-1.0) == "ABSTAINED"
    async with sessions_fixture() as session:
        async with session.begin():
            row = await session.get(Incident, inc.id)
            row.status_projection = "NEEDS_REVIEW"

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(local_token="inv-token")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            own = {"X-Tenant-Id": "alpha", "X-OpsBlaze-Token": "inv-token"}
            other = {"X-Tenant-Id": "beta", "X-OpsBlaze-Token": "inv-token"}

            cat = await client.get("/v1/tools/catalog", headers=own)
            assert cat.status_code == 200
            assert cat.json()["arbitrary_execution_enabled"] is False
            assert len(cat.json()["tools"]) == 2
            assert (await client.get("/v1/tools/catalog", headers={"X-Tenant-Id": "alpha", "X-OpsBlaze-Token": "bad"})).status_code == 401

            caps = await client.get(f"/v1/incidents/{inc.id}/capabilities", headers=own)
            assert caps.status_code == 200 and len(caps.json()["capabilities"]) == 1
            assert (await client.get(f"/v1/incidents/{inc.id}/capabilities", headers=other)).status_code == 404

            ev_page = await client.get(f"/v1/incidents/{inc.id}/evidence/page?limit=1", headers=own)
            assert ev_page.status_code == 200 and len(ev_page.json()["items"]) == 1
            assert ev_page.json()["items"][0]["freshness"] == "FRESH"
            assert (await client.get(f"/v1/incidents/{foreign.id}/evidence/page", headers=own)).status_code == 404

            hyp_page = await client.get(f"/v1/incidents/{inc.id}/hypotheses/page", headers=own)
            assert hyp_page.status_code == 200
            assert hyp_page.json()["items"][0]["decision"] == "ABSTAINED_TIMEOUT"

            manifests = await client.get(f"/v1/incidents/{inc.id}/context-manifests", headers=own)
            assert manifests.status_code == 200 and len(manifests.json()["manifests"]) == 1

            metrics = await client.get(f"/v1/incidents/{inc.id}/investigation/metrics", headers=own)
            assert metrics.status_code == 200
            assert metrics.json()["probe_calls_total"] == 1
            assert metrics.json()["capabilities_issued"] == 1

            # Authorized manual investigation trigger requires exact If-Match version.
            bad_ver = await client.post(
                f"/v1/incidents/{inc.id}/investigate",
                headers={**own, "If-Match": "999"},
            )
            assert bad_ver.status_code == 409
            ok_trigger = await client.post(
                f"/v1/incidents/{inc.id}/investigate",
                headers={**own, "If-Match": "1"},
            )
            assert ok_trigger.status_code == 202
            assert ok_trigger.json()["kind"] == "MANUAL_INVESTIGATE"
    finally:
        app.dependency_overrides.clear()

    async with sessions_fixture() as session:
        intents = (await session.scalars(
            select(OutboxEntry).where(
                OutboxEntry.incident_id == inc.id, OutboxEntry.kind == "MANUAL_INVESTIGATE",
            )
        )).all()
        assert len(intents) == 1
        assert (await session.scalars(select(ProbeCapability))).all()
