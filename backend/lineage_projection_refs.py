"""Canonical bounded causal edge identities, independent of storage."""

from uuid import UUID

from backend.lineage_refs import MAX_EVIDENCE_REFS, bounded_evidence_refs, reference_uuid

EDGE_SPECS = {
    "hypothesis": ("evidence", "CITES"),
    "plan": ("evidence", "JUSTIFIED_BY"),
    "approval": ("plan", "REVIEWS"),
    "outcome": ("effect", "WITNESSES"),
}


def evidence_candidates(values: object) -> set[UUID]:
    """Project only canonical references within the source's evidence budget."""
    visible, _, _ = bounded_evidence_refs(values, MAX_EVIDENCE_REFS)
    return {ref for value in visible if (ref := reference_uuid(value)) is not None}


def projection_status(expected: set[tuple[str, str]], recorded: set[tuple[str, str]], *, truncated: bool) -> str:
    """An incomplete read cannot certify cache agreement."""
    if truncated:
        return "NOT_CHECKED_TRUNCATED"
    return "MATCHED" if expected == recorded else "STALE_OR_MISSING"
