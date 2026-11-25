from uuid import uuid4

import pytest

from backend.lineage_refs import bounded_evidence_refs, reference_uuid


@pytest.mark.parametrize("value", [None, "private-value", {}, 42])
def test_malformed_evidence_list_is_not_iterated(value):
    assert bounded_evidence_refs(value, 200) == ([], False, False)


def test_evidence_budget_is_bounded_across_nodes():
    values = [str(uuid4())] * 1000
    assert bounded_evidence_refs(values, 7) == (values[:7], True, True)
    assert bounded_evidence_refs(values, 900) == (values[:200], True, True)
    assert bounded_evidence_refs(values, 0) == ([], True, True)
    assert bounded_evidence_refs([], 0) == ([], False, True)


def test_reference_requires_canonical_uuid():
    value = uuid4()
    assert reference_uuid(value) == value
    assert reference_uuid(str(value)) == value
    for invalid in (None, {}, 5, value.hex, "private-value", "0" * 10000):
        assert reference_uuid(invalid) is None
