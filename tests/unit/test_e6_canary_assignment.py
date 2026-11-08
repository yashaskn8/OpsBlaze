"""Unit tests for Feature E6 — Consistent Hashing and Canary Cohort Assignment."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from backend.domain.release import CanaryArm, ReleaseState, assign_incident_cohort


def test_consistent_hashing_repeatability() -> None:
    """Same input attributes always produce the identical arm and hash value."""
    tenant_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    target_id = "simulator-target-01"
    bundle_id = uuid.uuid4()
    incumbent_bundle_id = uuid.uuid4()

    release = SimpleNamespace(
        state=ReleaseState.CANARY.value,
        canary_percentage=25,
        rollout_salt="static-test-salt-1234",
        bundle_id=bundle_id,
        incumbent_bundle_id=incumbent_bundle_id,
        kill_switch_active=False,
    )

    arm1, b_id1, hash1 = assign_incident_cohort(
        tenant_id=tenant_id,
        incident_id=incident_id,
        target_id=target_id,
        release=release,
        default_incumbent_bundle_id=incumbent_bundle_id,
    )

    arm2, b_id2, hash2 = assign_incident_cohort(
        tenant_id=tenant_id,
        incident_id=incident_id,
        target_id=target_id,
        release=release,
        default_incumbent_bundle_id=incumbent_bundle_id,
    )

    assert arm1 == arm2
    assert b_id1 == b_id2
    assert hash1 == hash2


def test_shadow_mode_routes_to_incumbent() -> None:
    """When release is in SHADOW mode, live traffic is 100% routed to incumbent."""
    tenant_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    target_id = "target-01"
    bundle_id = uuid.uuid4()
    incumbent_bundle_id = uuid.uuid4()

    release = SimpleNamespace(
        state=ReleaseState.SHADOW.value,
        canary_percentage=0,
        rollout_salt="salt-1",
        bundle_id=bundle_id,
        incumbent_bundle_id=incumbent_bundle_id,
        kill_switch_active=False,
    )

    arm, b_id, _hash_val = assign_incident_cohort(
        tenant_id=tenant_id,
        incident_id=incident_id,
        target_id=target_id,
        release=release,
        default_incumbent_bundle_id=incumbent_bundle_id,
    )

    assert arm == CanaryArm.INCUMBENT
    assert b_id == incumbent_bundle_id


def test_active_mode_routes_to_candidate() -> None:
    """When release is in ACTIVE mode, traffic is 100% routed to candidate."""
    tenant_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    target_id = "target-01"
    bundle_id = uuid.uuid4()
    incumbent_bundle_id = uuid.uuid4()

    release = SimpleNamespace(
        state=ReleaseState.ACTIVE.value,
        canary_percentage=100,
        rollout_salt="salt-1",
        bundle_id=bundle_id,
        incumbent_bundle_id=incumbent_bundle_id,
        kill_switch_active=False,
    )

    arm, b_id, _hash_val = assign_incident_cohort(
        tenant_id=tenant_id,
        incident_id=incident_id,
        target_id=target_id,
        release=release,
        default_incumbent_bundle_id=incumbent_bundle_id,
    )

    assert arm == CanaryArm.CANDIDATE
    assert b_id == bundle_id


def test_kill_switch_forces_incumbent_routing() -> None:
    """If kill_switch_active is True, routing immediately falls back to incumbent."""
    tenant_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    target_id = "target-01"
    bundle_id = uuid.uuid4()
    incumbent_bundle_id = uuid.uuid4()

    release = SimpleNamespace(
        state=ReleaseState.CANARY.value,
        canary_percentage=50,
        rollout_salt="salt-1",
        bundle_id=bundle_id,
        incumbent_bundle_id=incumbent_bundle_id,
        kill_switch_active=True,  # Kill switch tripped
    )

    arm, b_id, _hash_val = assign_incident_cohort(
        tenant_id=tenant_id,
        incident_id=incident_id,
        target_id=target_id,
        release=release,
        default_incumbent_bundle_id=incumbent_bundle_id,
    )

    assert arm == CanaryArm.INCUMBENT
    assert b_id == incumbent_bundle_id


def test_canary_distribution_spread() -> None:
    """Test distribution of 1000 incidents with 20% canary percentage."""
    tenant_id = uuid.uuid4()
    target_id = "target-01"
    bundle_id = uuid.uuid4()
    incumbent_bundle_id = uuid.uuid4()

    release = SimpleNamespace(
        state=ReleaseState.CANARY.value,
        canary_percentage=20,
        rollout_salt="stat-salt-distribution",
        bundle_id=bundle_id,
        incumbent_bundle_id=incumbent_bundle_id,
        kill_switch_active=False,
    )

    candidate_count = 0
    total = 1000
    for _ in range(total):
        arm, _, _ = assign_incident_cohort(
            tenant_id=tenant_id,
            incident_id=uuid.uuid4(),
            target_id=target_id,
            release=release,
            default_incumbent_bundle_id=incumbent_bundle_id,
        )
        if arm == CanaryArm.CANDIDATE:
            candidate_count += 1

    ratio = candidate_count / total
    # Check that ratio is approximately 20% (between 16% and 24%)
    assert 0.16 <= ratio <= 0.24, f"Observed ratio {ratio} outside expected range [0.16, 0.24]"
