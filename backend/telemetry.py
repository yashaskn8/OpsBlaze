"""P3 bounded OpenTelemetry spans with an explicit safe attribute allowlist.

Tracing is opt-in and diagnostic only. It never conveys plan permissions,
recorded evidence, target names, user subjects, prompts, tokens or raw errors.
"""

from contextlib import contextmanager
from urllib.parse import urlparse
from uuid import UUID

from opentelemetry import trace

from backend.settings import Settings

TRACER = trace.get_tracer("opsblaze", schema_url="https://opentelemetry.io/schemas/1.27.0")

OPERATIONS = frozenset({
    "opsblaze.incident.admit",
    "opsblaze.outbox.deliver",
    "opsblaze.experience.capture",
    "opsblaze.outcome.observe",
    "opsblaze.incident.project",
    "opsblaze.incident.route",
    "opsblaze.incident.inspect",
    "opsblaze.incident.simulator_apply",
})


@contextmanager
def trace_operation(
    operation: str, *, incident_id: UUID | None = None,
    event_id: int | None = None,
    activity_schedule_to_start_ms: int | None = None,
):
    """Emit only approved identifiers; errors are never serialized in spans."""
    if operation not in OPERATIONS:
        raise ValueError("unrecognized diagnostic operation")
    if incident_id is not None and not isinstance(incident_id, UUID):
        raise ValueError("incident trace key must be UUID")
    if event_id is not None and (type(event_id) is not int or event_id < 1):
        raise ValueError("event trace key must be a positive integer")
    if activity_schedule_to_start_ms is not None and (
        type(activity_schedule_to_start_ms) is not int or activity_schedule_to_start_ms < 0
    ):
        raise ValueError("activity scheduling delay must be nonnegative milliseconds")
    with TRACER.start_as_current_span(
        operation, record_exception=False, set_status_on_exception=False,
    ) as span:
        span.set_attribute("opsblaze.component", operation.split(".")[1])
        if incident_id is not None:
            span.set_attribute("opsblaze.incident_id", str(incident_id))
        if event_id is not None:
            span.set_attribute("opsblaze.outbox_id", event_id)
        if activity_schedule_to_start_ms is not None:
            span.set_attribute("opsblaze.activity.schedule_to_start_ms", activity_schedule_to_start_ms)
        yield


def initialize_tracing(settings: Settings) -> bool:
    """Opt-in OTLP exporter with fail-closed endpoint validation.

    The exporter is optional until a collector is deliberately configured.
    A production collector must use HTTPS; local development may use
    loopback HTTP. This does not claim full Temporal trace propagation.
    """
    endpoint = settings.otel_traces_endpoint
    if not endpoint:
        return False
    parsed = urlparse(endpoint)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or not parsed.path.endswith("/v1/traces")
        or not (
            parsed.scheme == "https"
            or (settings.environment == "development"
                and parsed.scheme == "http" and loopback)
        )
    ):
        raise ValueError("OTLP endpoint must be an approved HTTPS traces URL")
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError as exc:
        raise RuntimeError(
            "install opsblaze[observability] to enable OTLP export"
        ) from exc
    provider = TracerProvider(
        resource=Resource.create({"service.name": "opsblaze"}),
        sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_ratio)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=3)),
    )
    trace.set_tracer_provider(provider)
    return True
