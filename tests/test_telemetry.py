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
