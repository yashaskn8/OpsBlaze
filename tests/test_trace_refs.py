"""Real SDK spans and durable trace references; no collector or external effect."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select

from backend.api import app
from backend.database import get_session
from backend.lineage import build_lineage, read_lineage_page
from backend.models import Incident, TraceRef, OutcomeRevision
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings, get_settings
from backend.trace_refs import read_trace, store_trace_ref


@pytest.fixture
def spans(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("backend.telemetry.TRACER", provider.get_tracer("test"))
    try:
        yield exporter
    finally:
        provider.shutdown()


@pytest.mark.asyncio
async def test_redteam_recorded_admission_link_and_scope(sessions_fixture, spans):
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "a", "trace", IncidentCreate(target_id="private"))
    async with sessions_fixture() as session:
        row = await session.scalar(select(TraceRef))
        assert row.record_id == incident.id
        context = spans.get_finished_spans()[0].context
        assert row.trace_id == f"{context.trace_id:032x}"
        assert row.span_id == f"{context.span_id:016x}"
        graph = await build_lineage(session, "a", incident.id)
        assert graph["trace_refs"][0]["node_id"] == f"incident:{incident.id}"
        assert graph["trace_spans_available"] is False
        assert graph["trace_refs"][0]["collector_verified"] is False
        trace = await read_trace(session, "a", incident.id, row.trace_id)
        assert trace["references"] == graph["trace_refs"]
        assert "private" not in str(trace)
        assert await read_trace(session, "b", incident.id, row.trace_id) is None
        assert await read_trace(session, "a", uuid4(), row.trace_id) is None
        trace_id = row.trace_id
    async with sessions_fixture() as session:
        replay = await admit_incident(session, "a", "trace", IncidentCreate(target_id="private"))
        assert replay.id == incident.id
    async with sessions_fixture() as session:
        assert len((await session.scalars(select(TraceRef))).all()) == 1

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(local_token="trace-test-token")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            path = f"/v1/incidents/{incident.id}/traces/{trace_id}"
            own = {"X-Tenant-Id": "a", "X-OpsBlaze-Token": "trace-test-token"}
            assert (await client.get(path, headers=own)).status_code == 200
            assert (await client.get(path, headers={**own, "X-Tenant-Id": "b"})).status_code == 404
            assert (await client.get(path, headers={**own, "X-OpsBlaze-Token": "wrong"})).status_code == 401
            assert (await client.get(path + "?limit=51", headers=own)).status_code == 422
            assert (await client.get(path.replace(trace_id, "0" * 32), headers=own)).status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_no_trace_provider_and_duplicate_optional_write(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "a", "no-span", IncidentCreate(target_id="checkout"))
    async with sessions_fixture() as session:
        assert (await session.scalars(select(TraceRef))).all() == []
    async with sessions_fixture() as session:
        async with session.begin():
            ids = ("a" * 32, "b" * 16)
            await store_trace_ref(session, "a", incident.id, "incident", incident.id, ids)
            # Duplicate reference must not abort the authoritative transaction.
            await store_trace_ref(session, "a", incident.id, "incident", incident.id, ("c" * 32, "d" * 16))
            row = await session.get(Incident, incident.id)
            row.probe_used = 1
    async with sessions_fixture() as session:
        row = await session.scalar(select(TraceRef))
        assert row.trace_id == "a" * 32
        assert (await session.get(Incident, incident.id)).probe_used == 1


@pytest.mark.asyncio
async def test_redteam_orphan_and_wrong_scope_trace_refs_are_not_returned(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "a", "own", IncidentCreate(target_id="checkout"))
    async with sessions_fixture() as session:
        foreign = await admit_incident(session, "b", "foreign", IncidentCreate(target_id="checkout"))
    async with sessions_fixture() as session:
        async with session.begin():
            outcome_id = uuid4()
            session.add(OutcomeRevision(
                id=outcome_id, tenant_id="b", incident_id=foreign.id,
                revision=1, label="DISPUTED", witness_source="SIMULATOR_WITNESS", reason="synthetic",
            ))
            session.add_all([
                TraceRef(tenant_id="a", incident_id=incident.id, record_type="outcome",
                         record_id=outcome_id, trace_id="a" * 32, span_id="b" * 16),
                TraceRef(tenant_id="a", incident_id=incident.id, record_type="incident",
                         record_id=foreign.id, trace_id="a" * 32, span_id="c" * 16),
                TraceRef(tenant_id="a", incident_id=incident.id, record_type="outcome",
                         record_id=uuid4(), trace_id="a" * 32, span_id="d" * 16),
            ])
    async with sessions_fixture() as session:
        assert await read_trace(session, "a", incident.id, "a" * 32) is None
        assert (await build_lineage(session, "a", incident.id))["trace_refs"] == []
        assert (await read_lineage_page(session, "a", incident.id, "outcome"))["trace_refs"] == []


@pytest.mark.asyncio
async def test_collector_outage_and_recovery_keep_authoritative_lineage_usable(
    sessions_fixture, monkeypatch,
):
    """P3-15: unreachable optional OTLP collector never degrades durable lineage."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import socket
    import threading
    import urllib.error
    import urllib.request

    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    # Reserve a loopback port, then close it so the collector is definitively absent.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    endpoint = f"http://127.0.0.1:{port}/v1/traces"

    class LoopbackHTTPExporter(SpanExporter):
        def export(self, spans):
            payload = ",".join(f"{s.context.trace_id:032x}" for s in spans).encode("utf-8")
            req = urllib.request.Request(endpoint, data=payload, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=0.3) as resp:  # nosec B310
                    return SpanExportResult.SUCCESS if resp.status == 200 else SpanExportResult.FAILURE
            except (urllib.error.URLError, TimeoutError, OSError):
                return SpanExportResult.FAILURE

        def shutdown(self):
            return None

    provider = TracerProvider()
    exporter = LoopbackHTTPExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("backend.telemetry.TRACER", provider.get_tracer("outage-test"))

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(local_token="outage-token")
    headers = {"X-Tenant-Id": "a", "X-OpsBlaze-Token": "outage-token"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            admitted = await client.post(
                "/v1/incidents",
                json={"target_id": "checkout"},
                headers={**headers, "Idempotency-Key": "collector-outage-1"},
            )
            assert admitted.status_code == 202, admitted.text
            incident_id = admitted.json()["id"]

            summary = await client.get(f"/v1/incidents/{incident_id}/lineage", headers=headers)
            assert summary.status_code == 200
            body = summary.json()
            assert body["projection"] == "COMPLETE"
            assert body["trace_spans_available"] is False
            assert body["trace_refs"][0]["collector_verified"] is False
            assert body["diagnostics"]["missing_edge_count"] == 0
            assert body["diagnostics"]["context_loss_count"] == 0

            page = await client.get(
                f"/v1/incidents/{incident_id}/lineage/records",
                params={"kind": "evidence"}, headers=headers,
            )
            assert page.status_code == 200
            assert page.json()["trace_spans_available"] is False

        # Start a local loopback receiver to verify collector recovery after outage.
        received = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append((self.path, self.rfile.read(length)))
                self.send_response(200)
                self.end_headers()

            def log_message(self, format, *args):
                return

        server = HTTPServer(("127.0.0.1", port), Receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                recovered = await client.post(
                    "/v1/incidents",
                    json={"target_id": "checkout"},
                    headers={**headers, "Idempotency-Key": "collector-recovered-2"},
                )
                assert recovered.status_code == 202
                second_id = recovered.json()["id"]
                second_graph = await client.get(f"/v1/incidents/{second_id}/lineage", headers=headers)
                assert second_graph.status_code == 200
                assert second_graph.json()["projection"] == "COMPLETE"
                assert second_graph.json()["trace_spans_available"] is False
            assert any(path == "/v1/traces" and payload for path, payload in received)
        finally:
            server.shutdown()
            server.server_close()
    finally:
        app.dependency_overrides.clear()
        provider.shutdown()


@pytest.mark.asyncio
async def test_cross_process_trace_continuity_links_admission_outbox_and_outcome(
    sessions_fixture, spans, monkeypatch,
):
    from backend.adapters.simulator import SimulatorAdapter
    from backend.effects.gateway import execute_simulator_effect
    from backend.effects.reservations import reserve_simulator_target
    from backend.models import ResourceEpoch, SimulatorTarget
    from backend.outcomes import adjudicate_simulator
    from backend.workers.dispatcher import dispatch_once

    enabled_settings = Settings(
        local_token="trace-continuity",
        otel_traces_endpoint="http://127.0.0.1:4318",
    )
    monkeypatch.setattr("backend.services.get_settings", lambda: enabled_settings)
    monkeypatch.setattr("backend.workers.dispatcher.get_settings", lambda: enabled_settings)
    monkeypatch.setattr("backend.outcomes.get_settings", lambda: enabled_settings)

    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(tenant_id="tc", target_id="checkout", version=1, healthy=False, last_fence=0))
            session.add(ResourceEpoch(tenant_id="tc", target_id="checkout", latest_fence=0, version_seen=1))
        incident = await admit_incident(session, "tc", "cont-1", IncidentCreate(target_id="checkout"))

    class StubTemporal:
        async def start_workflow(self, *args, **kwargs):
            return None

    assert await dispatch_once(StubTemporal(), sessions_fixture, tenant_filter="tc")
    permit = await reserve_simulator_target(sessions_fixture, "tc", "checkout", incident.id, "c" * 64)
    adapter = SimulatorAdapter(sessions_fixture)
    assert await execute_simulator_effect(sessions_fixture, adapter, permit) == "VERIFIED"
    async with sessions_fixture() as session:
        from backend.models import EffectReceipt
        receipt = await session.scalar(select(EffectReceipt).where(EffectReceipt.incident_id == incident.id))
    assert await adjudicate_simulator(sessions_fixture, adapter, "tc", incident.id, receipt.id) == "VERIFIED"

    async with sessions_fixture() as session:
        refs = (await session.scalars(select(TraceRef).where(TraceRef.incident_id == incident.id))).all()
        assert {r.record_type for r in refs} == {"incident", "outcome"}
        assert len({r.trace_id for r in refs}) == 1
        trace_id = refs[0].trace_id
        trace_doc = await read_trace(session, "tc", incident.id, trace_id)
        assert len(trace_doc["references"]) == 2
    finished = spans.get_finished_spans()
    trace_ids = {f"{s.context.trace_id:032x}" for s in finished}
    assert trace_id in trace_ids
    names = [s.name for s in finished]
    assert "opsblaze.incident.admit" in names
    assert "opsblaze.outbox.deliver" in names
    assert "opsblaze.outcome.observe" in names
