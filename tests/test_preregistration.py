"""G4 E5 signed preregistration integrity; test keys are generated in memory.

A valid signature does NOT certify custody, ordering in time or live policy.
"""

import base64
import hashlib
import json
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.evaluation.heldout import canonical_digest, main
from backend.evaluation.preregistration import (
    SCHEMA, PRIMARY_METRIC, STOPPING_RULE,
    registration_bytes, verify_preregistration,
)


def signed_fixture(expected_pairs=64):
    owner = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    trusted_pem = owner.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    other_pem = other.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    raw = owner.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    manifest = {
        "version": 1,
        "oracle_sha256": "a" * 64,
        "min_pairs": 64,
        "training_families": ["train-a"],
    }
    registration = {
        "schema": SCHEMA,
        "manifest_sha256": canonical_digest(manifest),
        "signer_public_key_sha256": hashlib.sha256(raw).hexdigest(),
        "signed_at_utc": "2026-11-22T08:00:00Z",
        "expected_pairs": expected_pairs,
        "primary_metric": PRIMARY_METRIC,
        "stopping_rule": STOPPING_RULE,
        "arm_versions": {"candidate": "candidate-v1", "incumbent": "runbook-v1"},
    }
    registration["signature"] = base64.urlsafe_b64encode(
        owner.sign(registration_bytes(registration))
    ).decode().rstrip("=")
    return manifest, registration, trusted_pem, other_pem


def test_valid_signed_evaluation_manifest_is_non_promotable():
    manifest, registration, trusted, _ = signed_fixture()
    proof = verify_preregistration(manifest, registration, trusted)
    assert proof["status"] == "VALID_SIGNATURE"
    assert proof["manifest_sha256"] == canonical_digest(manifest)
    assert proof["expected_pairs"] == 64
    assert proof["independent_custody_certified"] is False
    assert proof["trusted_timestamp_certified"] is False
    assert proof["promotion_eligible"] is False


@pytest.mark.parametrize("change", [
    "new_manifest", "old_trusted_key", "larger_sample",
    "new_signing_time", "different_primary_metric", "extra_unsigned_flag",
    "bad_signature", "different_arm_version",
])
def test_forged_or_rewritten_preregistration_is_rejected(change):
    manifest, registration, trusted, other = signed_fixture()
    if change == "new_manifest":
        manifest["min_pairs"] = 65
    elif change == "old_trusted_key":
        trusted = other
    elif change == "larger_sample":
        registration["expected_pairs"] = 65
    elif change == "new_signing_time":
        registration["signed_at_utc"] = "2026-11-23T08:00:00Z"
    elif change == "different_primary_metric":
        registration["primary_metric"] = "candidate_correct"
    elif change == "extra_unsigned_flag":
        registration["promotion_eligible"] = True
    elif change == "bad_signature":
        registration["signature"] = "!" * 86
    else:
        registration["arm_versions"]["candidate"] = "forged-model"
    with pytest.raises(ValueError, match="cannot be trusted"):
        verify_preregistration(manifest, registration, trusted)


def test_unverified_external_timestamp_never_grants_independence():
    manifest, registration, trusted, _ = signed_fixture()
    result = verify_preregistration(manifest, registration, trusted)
    assert result["trusted_timestamp_certified"] is False
    assert result["independent_custody_certified"] is False


def test_cli_requires_exact_signed_case_count_and_separate_key(
    tmp_path, monkeypatch, capsys,
):
    cases = [
        {
            "case_id": f"trial-{n:03d}",
            "family": "holdout-family",
            "expected": "PROPOSE" if n < 32 else "ABSTAIN",
            "negative_control": n >= 32,
        }
        for n in range(64)
    ]
    oracle = {"cases": cases, "oracle_version": "synthetic-only"}
    incumbent = [
        {
            "case_id": c["case_id"], "decision": "ABSTAIN",
            "probe_cost_units": 2, "latency_ms": 40,
        } for c in cases
    ]
    candidate = [
        {
            "case_id": c["case_id"], "decision": c["expected"],
            "probe_cost_units": 1, "latency_ms": 20,
        } for c in cases
    ]
    manifest, registration, trusted, _ = signed_fixture()
    manifest["oracle_sha256"] = canonical_digest(oracle)
    owner = Ed25519PrivateKey.generate()
    raw_key = owner.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trusted = owner.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    registration["manifest_sha256"] = canonical_digest(manifest)
    registration["signer_public_key_sha256"] = hashlib.sha256(raw_key).hexdigest()
    registration["signature"] = base64.urlsafe_b64encode(
        owner.sign(registration_bytes(registration))
    ).decode().rstrip("=")

    files = {
        "manifest.json": manifest,
        "oracle.json": oracle,
        "trials.json": {"candidate": candidate, "incumbent": incumbent},
        "preregistration.json": registration,
    }
    for filename, obj in files.items():
        (tmp_path / filename).write_text(json.dumps(obj), encoding="utf-8")
    (tmp_path / "trusted-key.pem").write_bytes(trusted)
    base = [
        "heldout", "--manifest", str(tmp_path / "manifest.json"),
        "--oracle", str(tmp_path / "oracle.json"),
        "--trials", str(tmp_path / "trials.json"),
        "--preregistration", str(tmp_path / "preregistration.json"),
        "--trusted-evaluator-key", str(tmp_path / "trusted-key.pem"),
    ]
    monkeypatch.setattr(sys, "argv", base)
    main()
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "OFFLINE_PASS"
    assert output["preregistration"]["status"] == "VALID_SIGNATURE"
    assert output["promotion_eligible"] is False

    # A signature covering an incorrect, fixed sample size cannot silently
    # drop or add cases, even when the signature itself is authentic.
    registration["expected_pairs"] = 65
    registration["signature"] = base64.urlsafe_b64encode(
        owner.sign(registration_bytes(registration))
    ).decode().rstrip("=")
    (tmp_path / "preregistration.json").write_text(
        json.dumps(registration), encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    denied = json.loads(capsys.readouterr().out)
    assert denied["state"] == "INVALID" and not denied["promotion_eligible"]

    monkeypatch.setattr(sys, "argv", base[:-2])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
