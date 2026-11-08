"""Phase 3.5 — State-machine property, transition, and canonical hashing unit tests for B1."""

from __future__ import annotations

import itertools
import random
import unicodedata
import uuid

import pytest

from backend.domain.incident import (
    ALLOWED_TRANSITIONS,
    CANCELLABLE_STATUSES,
    TERMINAL_STATUSES,
    IncidentCreateRequest,
    IncidentStatus,
    InvalidStateTransitionError,
    StaleStateVersionError,
    WorkflowIncidentInput,
    canonical_request_hash,
    make_incident_workflow_id,
    validate_client_key,
    validate_transition,
)
from backend.investigator.fake import FakeInvestigator


def test_every_legal_transition_succeeds() -> None:
    """Verify every transition declared in ALLOWED_TRANSITIONS is accepted."""
    for src, targets in ALLOWED_TRANSITIONS.items():
        for dst in targets:
            should_apply = validate_transition(
                current_status=src,
                target_status=dst,
                current_version=1,
                expected_version=1,
            )
            assert should_apply is True


def test_every_illegal_transition_rejected() -> None:
    """Exhaustively test all (src, dst) pairs not in ALLOWED_TRANSITIONS and not same-state."""
    all_states = list(IncidentStatus)
    for src, dst in itertools.product(all_states, all_states):
        if src == dst:
            continue
        if dst in ALLOWED_TRANSITIONS.get(src, frozenset()):
            continue
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(
                current_status=src,
                target_status=dst,
                current_version=2,
                expected_version=2,
            )


def test_terminal_states_reject_all_outgoing_transitions() -> None:
    """Verify no terminal state can transition to any other state, including CANCELLED."""
    for term in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[term] == frozenset()
        for target in IncidentStatus:
            if target == term:
                # Same-state idempotent check returns False
                assert (
                    validate_transition(
                        current_status=term,
                        target_status=target,
                        current_version=3,
                        expected_version=3,
                    )
                    is False
                )
            else:
                with pytest.raises(InvalidStateTransitionError):
                    validate_transition(
                        current_status=term,
                        target_status=target,
                        current_version=3,
                        expected_version=3,
                    )


def test_cancellable_vs_non_cancellable_states() -> None:
    """Verify ACCEPTED, DISPATCH_PENDING, INVESTIGATING, AWAITING_APPROVAL allow CANCELLED,
    while EXECUTING and VERIFYING do not allow unguarded B1 cancellation.
    """
    for state in CANCELLABLE_STATUSES:
        assert IncidentStatus.CANCELLED in ALLOWED_TRANSITIONS[state]
        assert validate_transition(state, IncidentStatus.CANCELLED, 1, 1) is True

    for non_cancel in (IncidentStatus.EXECUTING, IncidentStatus.VERIFYING):
        assert IncidentStatus.CANCELLED not in ALLOWED_TRANSITIONS[non_cancel]
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(non_cancel, IncidentStatus.CANCELLED, 4, 4)


def test_repeated_same_state_transition_is_idempotent_for_retries() -> None:
    """Verify repeated transition requests with matching or N-1 version are idempotent no-ops."""
    # Exact version match
    assert (
        validate_transition(
            IncidentStatus.INVESTIGATING,
            IncidentStatus.INVESTIGATING,
            current_version=2,
            expected_version=2,
        )
        is False
    )
    # Retry after lost ack where DB version already incremented from 1 to 2
    assert (
        validate_transition(
            IncidentStatus.INVESTIGATING,
            IncidentStatus.INVESTIGATING,
            current_version=2,
            expected_version=1,
        )
        is False
    )
    # Completely stale expected version must still fail
    with pytest.raises(StaleStateVersionError):
        validate_transition(
            IncidentStatus.INVESTIGATING,
            IncidentStatus.INVESTIGATING,
            current_version=5,
            expected_version=2,
        )


def test_invalid_and_stale_status_versions_rejected() -> None:
    """Verify version <= 0 or mismatched expected_version raises StaleStateVersionError."""
    with pytest.raises(StaleStateVersionError):
        validate_transition(
            IncidentStatus.ACCEPTED,
            IncidentStatus.DISPATCH_PENDING,
            current_version=0,
            expected_version=0,
        )
    with pytest.raises(StaleStateVersionError):
        validate_transition(
            IncidentStatus.ACCEPTED,
            IncidentStatus.DISPATCH_PENDING,
            current_version=2,
            expected_version=0,
        )
    with pytest.raises(StaleStateVersionError):
        validate_transition(
            IncidentStatus.ACCEPTED,
            IncidentStatus.DISPATCH_PENDING,
            current_version=3,
            expected_version=2,
        )


def test_property_random_transition_walks_never_bypass_guards() -> None:
    """Randomized property test: 500 random state walk sequences never allow illegal transitions
    or non-monotonic version updates.
    """
    rng = random.Random(20261107)
    all_states = list(IncidentStatus)

    for _ in range(500):
        state = IncidentStatus.ACCEPTED
        version = 1
        for _step in range(12):
            candidate = rng.choice(all_states)
            exp_ver = version if rng.random() < 0.8 else rng.randint(0, version + 3)

            if exp_ver < 1:
                with pytest.raises(StaleStateVersionError):
                    validate_transition(state, candidate, version, exp_ver)
                continue

            if candidate == state:
                if exp_ver in (version, version - 1):
                    assert validate_transition(state, candidate, version, exp_ver) is False
                else:
                    with pytest.raises(StaleStateVersionError):
                        validate_transition(state, candidate, version, exp_ver)
                continue

            if exp_ver != version:
                with pytest.raises(StaleStateVersionError):
                    validate_transition(state, candidate, version, exp_ver)
                continue

            if candidate in ALLOWED_TRANSITIONS[state]:
                assert validate_transition(state, candidate, version, exp_ver) is True
                state = candidate
                version += 1
            else:
                with pytest.raises(InvalidStateTransitionError):
                    validate_transition(state, candidate, version, exp_ver)


def test_canonical_request_hash_unicode_and_field_order_invariance() -> None:
    """Verify Unicode NFC/NFD equivalence, JSON key order invariance, and null vs missing optional fields."""
    # "café" in NFC vs NFD
    nfc_title = unicodedata.normalize("NFC", "Primary DB café lag")
    nfd_title = unicodedata.normalize("NFD", "Primary DB café lag")
    assert nfc_title != nfd_title

    req1 = IncidentCreateRequest(
        target_id="sim-db-1",
        title=nfc_title,
        severity="SEV1",
        labels={"zone": "ap-south-1", "service": "orders"},
    )
    req2 = IncidentCreateRequest(
        severity="SEV1",
        target_id="sim-db-1",
        title=nfd_title,
        description=None,
        labels={"service": "orders", "zone": "ap-south-1"},
    )
    h1 = canonical_request_hash(req1.to_canonical_payload())
    h2 = canonical_request_hash(req2.to_canonical_payload())
    assert h1 == h2


def test_validate_client_key_rejects_empty_and_malformed_keys() -> None:
    for bad_key in ["", "   ", "\t\n", "bad\x00key", "bad\nkey", "x" * 129]:
        with pytest.raises(ValueError):
            validate_client_key(bad_key)

    # Unicode NFC normalization on client key
    k_nfc = validate_client_key(unicodedata.normalize("NFC", "key-é-1"))
    k_nfd = validate_client_key(unicodedata.normalize("NFD", "key-é-1"))
    assert k_nfc == k_nfd


def test_fake_investigator_determinism_and_read_only_contract() -> None:
    inv = FakeInvestigator()
    tenant_id = str(uuid.uuid4())
    incident_id = str(uuid.uuid4())
    wf_in = WorkflowIncidentInput(
        tenant_id=tenant_id,
        incident_id=incident_id,
        target_id="sim-redis-cluster",
        request_sha256="a" * 64,
    )
    res1 = inv.investigate(wf_in)
    res2 = inv.investigate(wf_in)
    assert res1 == res2
    assert res1.recommended_status == IncidentStatus.AWAITING_APPROVAL
    assert res1.requires_human_approval is True
    assert len(res1.observations) == 2
    assert all(o.read_only is True for o in res1.observations)
    assert make_incident_workflow_id(uuid.UUID(tenant_id), uuid.UUID(incident_id)) == (
        f"incident:{tenant_id}:{incident_id}"
    )
