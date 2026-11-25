from uuid import UUID

import pytest

from backend.lineage_projection_refs import evidence_candidates, projection_status


def test_canonical_projection_is_deduplicated_and_bounded():
    values = [str(UUID(int=i)) for i in range(1, 203)]
    assert evidence_candidates(values) == {UUID(int=i) for i in range(1, 201)}
    assert evidence_candidates([values[0], values[0], {"private": "data"}, "bad"]) == {UUID(int=1)}


@pytest.mark.parametrize("value", [None, 123, {}, "private"])
def test_corrupt_source_never_creates_edges(value):
    assert evidence_candidates(value) == set()


@pytest.mark.parametrize("expected,recorded,truncated,result", [
    ({("a", "b")}, {("a", "b")}, False, "MATCHED"),
    ({("a", "b")}, set(), False, "STALE_OR_MISSING"),
    (set(), {("a", "b")}, False, "STALE_OR_MISSING"),
    ({("a", "b")}, {("a", "c")}, False, "STALE_OR_MISSING"),
    (set(), set(), True, "NOT_CHECKED_TRUNCATED"),
])
def test_projection_comparison(expected, recorded, truncated, result):
    assert projection_status(expected, recorded, truncated=truncated) == result
