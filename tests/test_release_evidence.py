"""G5 independent-key review evidence without any promotion authority."""

import base64
import hashlib
import json
import sys
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.evaluation.heldout import canonical_digest
from backend.policy.release_evidence import (
    ACTION, ROLES, main, review_bytes, verify_release_evidence,
)


def review_fixture():
    bundle = {
        "policy_content": {
            "candidate_id": "synthetic-candidate",
            "tool_catalog": "simulator-readonly-v1",
            "model_id": "none",
        },
        "source_commit": "1" * 40,
        "tool_manifest_digest": "2" * 64,
    }
    bundle["policy_digest"] = canonical_digest(bundle["policy_content"])
    evaluation = {
        "state": "OFFLINE_PASS",
        "promotion_eligible": False,
        "recovery_cost": {
            "promotion_eligible": False,
            "measured_in_lab_only": True,
            "pairs": 64,
        },
        "preregistration": {
            "status": "VALID_SIGNATURE",
            "promotion_eligible": False,
        },
    }
    keys, public = {}, {}
    for role in ROLES:
        key = Ed25519PrivateKey.generate()
        keys[role] = key
        public[role] = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    reviews = []
    for role in ROLES:
        raw = keys[role].public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        record = {
            "version": 1,
            "role": role,
            "policy_digest": bundle["policy_digest"],
            "evaluation_digest": canonical_digest(evaluation),
            "source_commit": bundle["source_commit"],
            "tool_manifest_digest": bundle["tool_manifest_digest"],
            "signer_key_sha256": hashlib.sha256(raw).hexdigest(),
            "review_action": ACTION,
            "signed_at_utc": "2026-11-22T08:00:00Z",
        }
        record["signature"] = base64.urlsafe_b64encode(
            keys[role].sign(review_bytes(record))
        ).decode().rstrip("=")
        reviews.append(record)
    return bundle, evaluation, reviews, public


def test_distinct_pinned_review_keys_bind_one_frozen_evidence_set():
    bundle, evaluation, reviews, keys = review_fixture()
    report = verify_release_evidence(bundle, evaluation, reviews, keys)
    assert report["state"] == "REVIEW_SIGNATURES_VALID_ONLY"
    assert len(set(report["reviewer_key_fingerprints"].values())) == 3
    assert report["evaluation_digest"] == canonical_digest(evaluation)
    assert report["production_gate_passed"] is False
    assert report["external_role_custody_certified"] is False
    assert report["trusted_timestamp_certified"] is False
    assert report["deployment_authorized"] is False
    assert report["promotion_eligible"] is False


@pytest.mark.parametrize("change", [
    "bundle_content", "bundle_digest", "report", "wrong_key", "duplicate_role",
    "missing_role", "same_actor", "changed_action", "changed_digest",
    "changed_commit", "changed_timestamp", "extra_field", "bad_signature",
    "claimed_promotion", "missing_preregistration", "no_recovery_measurements",
])
def test_tampered_or_untrusted_release_evidence_is_denied(change):
    bundle, evaluation, reviews, trusted = review_fixture()
    if change == "bundle_content":
        bundle["policy_content"]["tool_catalog"] = "unreviewed-external-adapter"
    elif change == "bundle_digest":
        bundle["policy_digest"] = "f" * 64
    elif change == "report":
        evaluation["recovery_cost"]["pairs"] = 12
    elif change == "wrong_key":
        trusted["release_owner"] = trusted["evaluator"]
    elif change == "duplicate_role":
        reviews[-1] = deepcopy(reviews[0])
    elif change == "missing_role":
        reviews.pop()
    elif change == "same_actor":
        trusted["security_reviewer"] = trusted["evaluator"]
    elif change == "changed_action":
        reviews[0]["review_action"] = "APPROVE_DEPLOYMENT"
    elif change == "changed_digest":
        reviews[1]["evaluation_digest"] = "f" * 64
    elif change == "changed_commit":
        reviews[2]["source_commit"] = "f" * 40
    elif change == "changed_timestamp":
        reviews[0]["signed_at_utc"] = "2026-11-23T08:00:00Z"
    elif change == "extra_field":
        reviews[1]["override_gate"] = True
    elif change == "bad_signature":
        reviews[0]["signature"] = "!" * 86
    elif change == "claimed_promotion":
        evaluation["promotion_eligible"] = True
    elif change == "missing_preregistration":
        evaluation["preregistration"]["status"] = "UNSIGNED_LAB_ONLY"
    else:
        evaluation.pop("recovery_cost")
    with pytest.raises(ValueError, match="invalid"):
        verify_release_evidence(bundle, evaluation, reviews, trusted)


def test_cli_produces_only_non_authorizing_review_receipt(tmp_path, monkeypatch, capsys):
    bundle, evaluation, reviews, trusted = review_fixture()
    for name, data in [
        ("bundle", bundle), ("evaluation", evaluation),
        ("reviews", {"reviews": reviews}),
    ]:
        (tmp_path / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    for role in ROLES:
        (tmp_path / f"{role}.pem").write_bytes(trusted[role])
    args = [
        "release-evidence",
        "--bundle", str(tmp_path / "bundle.json"),
        "--evaluation", str(tmp_path / "evaluation.json"),
        "--reviews", str(tmp_path / "reviews.json"),
        "--evaluator-key", str(tmp_path / "evaluator.pem"),
        "--security-key", str(tmp_path / "security_reviewer.pem"),
        "--release-owner-key", str(tmp_path / "release_owner.pem"),
    ]
    monkeypatch.setattr(sys, "argv", args)
    main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["state"] == "REVIEW_SIGNATURES_VALID_ONLY"
    assert not receipt["deployment_authorized"]

    reviews[0]["evaluation_digest"] = "f" * 64
    (tmp_path / "reviews.json").write_text(
        json.dumps({"reviews": reviews}), encoding="utf-8",
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    denied = json.loads(capsys.readouterr().out)
    assert denied == {
        "state": "INVALID",
        "deployment_authorized": False,
        "promotion_eligible": False,
    }
