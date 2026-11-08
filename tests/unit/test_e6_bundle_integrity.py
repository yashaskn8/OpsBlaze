"""Unit tests for Feature E6 — Policy Bundle Canonicalization, Digest, and Signature Integrity."""

from __future__ import annotations

import uuid

import pytest

from backend.domain.release import (
    BundleNotSealedError,
    canonicalize_bundle_payload,
    compute_bundle_digest,
    compute_bundle_signature,
    verify_bundle_signature,
)
from backend.releases.bundle_service import PolicyBundleService


def test_rfc8785_canonicalization_determinism() -> None:
    """Canonicalization must be deterministic regardless of dictionary key insertion order."""
    dict_a = {
        "b": 2,
        "a": 1,
        "nested": {"z": "end", "m": "middle"},
        "list": [1, 2, 3],
    }
    dict_b = {
        "list": [1, 2, 3],
        "nested": {"m": "middle", "z": "end"},
        "a": 1,
        "b": 2,
    }

    bytes_a = canonicalize_bundle_payload(dict_a)
    bytes_b = canonicalize_bundle_payload(dict_b)

    assert bytes_a == bytes_b
    assert bytes_a.decode("utf-8") == '{"a":1,"b":2,"list":[1,2,3],"nested":{"m":"middle","z":"end"}}'


def test_bundle_digest_and_signature_verification() -> None:
    """Digest and signature verify correctly with expected secret and key ID."""
    payload = {"bundle_name": "test-v1", "model_id": "model-a", "version": 1}
    canonical_bytes = canonicalize_bundle_payload(payload)
    digest = compute_bundle_digest(canonical_bytes)

    secret = "test-secret-key-32-bytes-long!!"
    key_id = "test-key-1"

    sig = compute_bundle_signature(digest, secret, key_id)
    assert verify_bundle_signature(digest, sig, secret, key_id) is True

    # Tampered digest should fail
    tampered_digest = "a" * 64
    assert verify_bundle_signature(tampered_digest, sig, secret, key_id) is False

    # Wrong secret should fail
    assert verify_bundle_signature(digest, sig, "wrong-secret", key_id) is False

    # Wrong key_id should fail
    assert verify_bundle_signature(digest, sig, secret, "wrong-key-id") is False


def test_bundle_tampering_detection() -> None:
    """Modifying bundle payload after computing digest triggers mismatch."""
    original_dict = {
        "tenant_id": str(uuid.uuid4()),
        "bundle_name": "planner-candidate-v2",
        "base_policy_version": "v1.0.0",
        "target_component": "investigator",
        "proposal_diff": {"action": "tighten_budget"},
        "prompt_templates": {"system": "Standard prompt"},
        "active_lesson_ids": ["L-001"],
        "model_id": "model-1",
        "model_version": "1.0",
        "tool_catalog_version": "v1",
        "a2_grant_version": "v1",
        "source_manifest_id": None,
        "source_report_id": None,
        "source_provenance_digest": "0" * 64,
    }
    orig_bytes = canonicalize_bundle_payload(original_dict)
    valid_digest = compute_bundle_digest(orig_bytes)

    # Tamper with prompt_templates
    tampered_dict = dict(original_dict)
    tampered_dict["prompt_templates"] = {"system": "TAMPERED prompt"}
    tampered_bytes = canonicalize_bundle_payload(tampered_dict)
    tampered_digest = compute_bundle_digest(tampered_bytes)

    assert tampered_digest != valid_digest


@pytest.mark.asyncio
async def test_bundle_service_seal_and_verify(session_factory) -> None:
    """Full lifecycle: create DRAFT bundle -> verify unsealed cannot be verified -> seal -> verify."""
    tenant_id = uuid.uuid4()
    bundle_svc = PolicyBundleService(session_factory)

    # Ensure tenant exists
    from backend.persistence.models import Tenant
    async with session_factory() as session:
        t = Tenant(id=tenant_id, name=f"test-tenant-bundle-{tenant_id}")
        session.add(t)
        await session.commit()

    bundle = await bundle_svc.create_bundle(
        tenant_id=tenant_id,
        bundle_name="test-bundle-seal",
        base_policy_version="v1.0.0",
        target_component="investigator",
        created_by="engineer-1",
        model_id="test-model",
        model_version="1.0",
        tool_catalog_version="v1",
        a2_grant_version="v1",
        source_provenance_digest="1" * 64,
    )
    assert bundle.status == "DRAFT"

    # Verifying an unsealed bundle raises BundleNotSealedError
    with pytest.raises(BundleNotSealedError):
        await bundle_svc.verify_bundle_integrity(tenant_id=tenant_id, bundle=bundle)

    # Seal the bundle
    sealed = await bundle_svc.seal_bundle(tenant_id=tenant_id, bundle_id=bundle.id)
    assert sealed.status == "SEALED"
    assert sealed.sealed_at is not None
    assert sealed.bundle_sha256 != "0" * 64

    # Integrity verification passes
    valid = await bundle_svc.verify_bundle_integrity(tenant_id=tenant_id, bundle=sealed)
    assert valid is True
