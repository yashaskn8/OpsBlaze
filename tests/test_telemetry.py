"""P3 redacted OpenTelemetry spans; no secrets or untrusted metadata."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings
from backend.telemetry import initialize_tracing, trace_operation
from backend.workers.dispatcher import dispatch_once


@pytest.mark.asyncio
async def test_redteam_collected_spans_correlate_incident_without_sensitive_labels(
    sessions_fixture, monkeypatch,
):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        "backend.telemetry.TRACER", provider.get_tracer("opsblaze"),
    )
    try:
        async with sessions_fixture() as session:
            incident = await admit_incident(
                session, "secret-tenant", "admit",
                IncidentCreate(target_id="confidential-target"),
            )
        with patch(
            "backend.workers.dispatcher.deliver_intent", new_callable=AsyncMock,
        ):
            assert await dispatch_once(AsyncMock(), sessions_fixture)

        spans = exporter.get_finished_spans()
        assert {span.name for span in spans} >= {
            "opsblaze.incident.admit", "opsblaze.outbox.deliver",
        }
        for span in spans:
            attrs = dict(span.attributes)
            assert set(attrs) <= {
                "opsblaze.component", "opsblaze.incident_id",
                "opsblaze.outbox_id",
            }
            assert attrs["opsblaze.incident_id"] == str(incident.id)
            assert not span.events
            dumped = repr(attrs) + repr(span.events)
            for secret in ("secret-tenant", "confidential-target", "admit"):
                assert secret not in dumped
    finally:
        provider.shutdown()


def test_redteam_trace_allowlist_prevents_custom_secret_fields():
    with pytest.raises(ValueError, match="unrecognized"):
        with trace_operation("debug.private-target-token"):
            pass
    with pytest.raises(ValueError, match="UUID"):
        with trace_operation(
            "opsblaze.incident.admit", incident_id="private-user",
        ):
            pass
    with pytest.raises(ValueError, match="positive"):
        with trace_operation("opsblaze.outbox.deliver", event_id=-1):
            pass


def test_redteam_tracing_endpoint_only_allows_tls_or_loopback_development():
    assert initialize_tracing(Settings()) is False
    for endpoint, environment in [
        ("http://collector.example.test/v1/traces", "production"),
        ("http://collector.example.test/v1/traces", "development"),
        ("https://user:password@collector.example.test/v1/traces", "production"),
        ("https://collector.example.test/v1/traces?token=private", "production"),
        ("https://collector.example.test/not-traces", "production"),
        ("file:///private/collector/v1/traces", "production"),
    ]:
        with pytest.raises(ValueError, match="approved HTTPS"):
            initialize_tracing(Settings(
                environment=environment, otel_traces_endpoint=endpoint,
            ))
    with pytest.raises(ValidationError):
        Settings(otel_sample_ratio=2)
    with pytest.raises(ValidationError):
        Settings(otel_sample_ratio=-1)


def test_redteam_trace_exception_text_not_automatically_exported(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("backend.telemetry.TRACER", provider.get_tracer("opsblaze"))
    try:
        with pytest.raises(RuntimeError):
            with trace_operation(
                "opsblaze.experience.capture", incident_id=uuid4(),
            ):
                raise RuntimeError("secret=must-not-export-this")
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert not spans[0].events
        assert "must-not-export-this" not in str(spans[0].attributes)
    finally:
        provider.shutdown()


def test_redteam_activity_span_contains_only_attempt_delay_and_safe_identity(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from backend.workflows import activities

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("backend.telemetry.TRACER", provider.get_tracer("opsblaze"))
    started = datetime.now(timezone.utc)
    monkeypatch.setattr(activities.activity, "in_activity", lambda: True)
    monkeypatch.setattr(activities.activity, "info", lambda: SimpleNamespace(
        current_attempt_scheduled_time=started - timedelta(milliseconds=125),
        started_time=started,
        workflow_id="private-tenant",
    ))
    incident_id = uuid4()
    try:
        with activities._trace_activity("opsblaze.incident.route", str(incident_id)):
            pass
        span = exporter.get_finished_spans()[0]
        assert dict(span.attributes) == {
            "opsblaze.component": "incident",
            "opsblaze.incident_id": str(incident_id),
            "opsblaze.activity.schedule_to_start_ms": 125,
        }
        assert not span.events
        monkeypatch.setattr(activities.activity, "in_activity", lambda: False)
        with activities._trace_activity("opsblaze.incident.route", str(incident_id)):
            pass
        assert "opsblaze.activity.schedule_to_start_ms" not in exporter.get_finished_spans()[1].attributes
    finally:
        provider.shutdown()


@pytest.mark.asyncio
async def test_redteam_admission_parent_correlates_dispatch_and_projection_without_header_trust(
    sessions_fixture, monkeypatch,
):
    from backend.workflows import activities
    from backend.trace_refs import admission_parent
    from opentelemetry.trace import get_current_span

    api = TracerProvider()
    worker = TracerProvider()
    api_export = InMemorySpanExporter()
    worker_export = InMemorySpanExporter()
    api.add_span_processor(SimpleSpanProcessor(api_export))
    worker.add_span_processor(SimpleSpanProcessor(worker_export))
    settings = Settings(otel_traces_endpoint="http://localhost:4318/v1/traces")
    monkeypatch.setattr("backend.workers.dispatcher.get_settings", lambda: settings)
    monkeypatch.setattr(activities, "get_settings", lambda: settings)
    monkeypatch.setattr(activities, "get_session_maker", lambda: sessions_fixture)
    try:
        monkeypatch.setattr("backend.telemetry.TRACER", api.get_tracer("api"))
        with api.get_tracer("untrusted").start_as_current_span("ambient") as ambient:
            async with sessions_fixture() as session:
                incident = await admit_incident(
                    session, "scope", "correlation", IncidentCreate(target_id="private"),
                )
        admission = next(span for span in api_export.get_finished_spans()
                         if span.name == "opsblaze.incident.admit")
        assert admission.parent is None
        assert admission.context.trace_id != ambient.get_span_context().trace_id
        monkeypatch.setattr("backend.telemetry.TRACER", worker.get_tracer("worker"))
        with patch("backend.workers.dispatcher.deliver_intent", new_callable=AsyncMock) as deliver:
            assert await dispatch_once(AsyncMock(), sessions_fixture)
            deliver.assert_awaited_once()
        await activities.project_incident_status("scope", str(incident.id), "INVESTIGATING")
        spans = worker_export.get_finished_spans()
        assert {span.name for span in spans} == {
            "opsblaze.outbox.deliver", "opsblaze.incident.project",
        }
        assert all(span.context.trace_id == admission.context.trace_id for span in spans)
        assert all(span.parent.span_id == admission.context.span_id for span in spans)
        foreign = await admission_parent(sessions_fixture, "wrong", incident.id, enabled=True)
        assert not get_current_span(foreign).get_span_context().is_valid
    finally:
        api.shutdown()
        worker.shutdown()


@pytest.mark.asyncio
async def test_optional_parent_read_failure_and_disabled_mode_are_root_contexts():
    from contextlib import asynccontextmanager
    from opentelemetry.trace import get_current_span
    from sqlalchemy.exc import OperationalError
    from backend.trace_refs import admission_parent

    calls = []

    @asynccontextmanager
    async def unavailable():
        calls.append(True)
        raise OperationalError("private", {}, RuntimeError("offline"))
        yield

    disabled = await admission_parent(unavailable, "scope", uuid4(), enabled=False)
    assert calls == []
    failed = await admission_parent(unavailable, "scope", uuid4(), enabled=True)
    assert calls == [True]
    for context in (disabled, failed):
        assert not get_current_span(context).get_span_context().is_valid
