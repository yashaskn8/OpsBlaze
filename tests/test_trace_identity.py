from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON
from opentelemetry.trace import INVALID_SPAN, NonRecordingSpan, SpanContext, TraceFlags
import pytest

from backend.trace_identity import recorded_span_ids, valid_hex_id


@pytest.mark.parametrize("value", [None, "0" * 32, "A" * 32, "g" * 32, "a" * 31, "a" * 33, 123])
def test_trace_identifier_rejects_invalid_input(value):
    assert not valid_hex_id(value, 32)


def test_recorded_id_matches_real_sampled_span():
    provider = TracerProvider(sampler=ALWAYS_ON)
    try:
        with provider.get_tracer("test").start_as_current_span("synthetic") as span:
            context = span.get_span_context()
            assert recorded_span_ids(span) == (f"{context.trace_id:032x}", f"{context.span_id:016x}")
        assert recorded_span_ids(span) is None
    finally:
        provider.shutdown()


def test_unsampled_or_remote_nonrecording_context_is_not_evidence():
    provider = TracerProvider(sampler=ALWAYS_OFF)
    try:
        with provider.get_tracer("test").start_as_current_span("synthetic") as span:
            assert recorded_span_ids(span) is None
    finally:
        provider.shutdown()
    assert recorded_span_ids(INVALID_SPAN) is None
    remote = NonRecordingSpan(SpanContext(1, 2, True, TraceFlags(TraceFlags.SAMPLED)))
    assert recorded_span_ids(remote) is None
