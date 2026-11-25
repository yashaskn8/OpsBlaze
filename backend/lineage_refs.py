"""Bound and redact untrusted durable evidence references for P3 graphs."""

from uuid import UUID

MAX_EVIDENCE_REFS = 200


def bounded_evidence_refs(values: object, remaining: int) -> tuple[list, bool, bool]:
    """Return a bounded list, whether omitted entries remain, and list validity."""
    if not isinstance(values, list):
        return [], False, False
    limit = min(MAX_EVIDENCE_REFS, max(0, remaining))
    return values[:limit], len(values) > limit, True


def reference_uuid(value: object) -> UUID | None:
    """Accept canonical UUID strings or native UUIDs; never echo other data."""
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None
