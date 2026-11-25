"""Strict, diagnostic-only identities for locally recorded OpenTelemetry spans."""

import re

from opentelemetry.context import Context
from opentelemetry.trace import NonRecordingSpan, Span, SpanContext, TraceFlags, set_span_in_context


def valid_hex_id(value: object, width: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == width
        and re.fullmatch(r"[0-9a-f]+", value) is not None
        and value != "0" * width
    )


def recorded_span_ids(span: Span) -> tuple[str, str] | None:
    """Never invent IDs or confuse an unsampled context with a recorded span."""
    context = span.get_span_context()
    if not span.is_recording() or not context.is_valid or not context.trace_flags.sampled:
        return None
    trace_id = f"{context.trace_id:032x}"
    span_id = f"{context.span_id:016x}"
    if not valid_hex_id(trace_id, 32) or not valid_hex_id(span_id, 16):
        return None
    return trace_id, span_id


def parent_context(trace_id: object, span_id: object) -> Context:
    """Reconstruct a sampled server-side parent or explicitly start a new root."""
    if not valid_hex_id(trace_id, 32) or not valid_hex_id(span_id, 16):
        return Context()
    parent = SpanContext(
        trace_id=int(trace_id, 16), span_id=int(span_id, 16), is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return set_span_in_context(NonRecordingSpan(parent), Context())
